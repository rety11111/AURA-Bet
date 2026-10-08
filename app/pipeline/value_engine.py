"""Value Engine (Модуль 5) — чистая математика: implied, edge, Келли, отбор сигналов.

Все функции здесь детерминированные и покрыты юнит-тестами (tests/test_value_engine.py):

1. p_implied = (1/кэф) / Σ(1/кэф_j) по всем исходам рынка — считается ПРОТИВ ЛУЧШЕЙ
   цены из агрегатора (см. sources/odds_aggregator.py).
2. Исходы 1X2/П1П2: edge = p_final − p_implied.
3. Тоталы: p_over(line) = 1 − Φ((line − mu)/sigma); under симметрично.
4. Форы: фора −h хозяевам заходит при margin > h → p = 1 − Φ((h − margin_mu)/margin_sigma);
   плюсовые — симметрично.
5. Двойной шанс: p = p1 + px (или p1 + p2, x2) и сравнение с implied.

Условия создания сигнала (ОДНОВРЕМЕННО):
  edge ≥ VALUE_THRESHOLD (лайв — VALUE_THRESHOLD_LIVE),
  score ≥ CONFIDENCE_MIN_SCORE,
  data_quality ≠ "weak",
  кэф ∈ [ODDS_MIN, ODDS_MAX],
  линия не suspicious.
Размер ставки: дробный Келли с жёстким кэпом и округлением до 0.25%.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from app.config import settings
from app.db.models import DataQuality, Market, Selection
from app.sources.odds_aggregator import AggregatedOutcome
from app.stats_models.base import prob_handicap, prob_over, prob_under

ODDS_ROUND_STEP = 0.005


@dataclass
class MarketProbabilities:
    """Все вероятности/параметры, из которых считаются value-сравнения."""

    prob_home: float
    prob_away: float
    prob_draw: float | None = None
    expected_total: float = 0.0
    total_sigma: float = 1.0
    expected_margin: float = 0.0
    margin_sigma: float = 1.0
    llm_confidence: float = 0.5
    key_factors: list[str] = field(default_factory=list)
    risk_notes: list[str] = field(default_factory=list)
    reasoning: str | None = None
    source: str = "ensemble"          # ensemble | llm | stat | judge_adjusted
    adjusted_by_judge: bool = False

    def probability_for(self, market: str, selection: str, line: float | None) -> float | None:
        if market in (Market.ONE_X_TWO,):
            if selection == Selection.HOME:
                return self.prob_home
            if selection == Selection.AWAY:
                return self.prob_away
            if selection == Selection.DRAW:
                return self.prob_draw
            return None
        if market == Market.TOTALS:
            if line is None:
                return None
            if selection == Selection.OVER:
                return prob_over(line, self.expected_total, self.total_sigma)
            if selection == Selection.UNDER:
                return prob_under(line, self.expected_total, self.total_sigma)
            return None
        if market == Market.HANDICAP:
            if line is None:
                return None
            if selection == Selection.HOME_HANDICAP:
                return prob_handicap(line, self.expected_margin, self.margin_sigma)
            if selection == Selection.AWAY_HANDICAP:
                # Фора гостям задаётся от их лица: line = +1.5 → гости покрывают,
                # если margin < 1.5, то есть 1 − P(margin > line) = 1 − prob_handicap(−line).
                return 1.0 - prob_handicap(-line, self.expected_margin, self.margin_sigma)
            return None
        if market == Market.DOUBLE_CHANCE:
            if self.prob_draw is None:
                return None
            if selection == Selection.DC_1X:
                return self.prob_home + self.prob_draw
            if selection == Selection.DC_X2:
                return self.prob_away + self.prob_draw
            if selection == Selection.DC_12:
                return self.prob_home + self.prob_away
            return None
        return None


# --------------------------------------------------------------------------- #
# Базовая математика
# --------------------------------------------------------------------------- #
def implied_probabilities(prices: list[float]) -> list[float]:
    """Нормализованные implied-вероятности рынка (удаление маржи пропорционально)."""
    clean = [price for price in prices if price and price > 1.0]
    if not clean:
        return []
    inverses = [1.0 / price for price in clean]
    total = sum(inverses)
    if total <= 0:
        return []
    return [value / total for value in inverses]


def implied_probability(price: float, market_prices: list[float]) -> float:
    """p_implied для конкретной цены внутри рынка."""
    if not price or price <= 1.0:
        return 0.0
    normalized = implied_probabilities(market_prices)
    if not normalized:
        return 0.0
    clean = [p for p in market_prices if p and p > 1.0]
    index = clean.index(price) if price in clean else None
    if index is None:
        return 0.0
    return normalized[index]


def edge(prob_final: float, prob_implied: float) -> float:
    return prob_final - prob_implied


def kelly_stake_pct(prob_final: float, odds: float, fraction: float | None = None, cap: float | None = None) -> float:
    """Дробный Келли в процентах банка, с жёстким кэпом и округлением до 0.25%."""
    fraction = settings.kelly_fraction if fraction is None else fraction
    cap = settings.stake_cap_pct if cap is None else cap
    if odds <= 1.0 or prob_final <= 0:
        return 0.0
    full_kelly = (prob_final * odds - 1.0) / (odds - 1.0)
    if full_kelly <= 0:
        return 0.0
    stake = fraction * full_kelly * 100.0
    stake = min(stake, cap)
    step = settings.stake_round_step
    if step > 0:
        stake = math.floor(stake / step) * step
    return round(max(0.0, stake), 2)


def market_prices_for(outcome: AggregatedOutcome, outcomes: list[AggregatedOutcome]) -> list[float]:
    """Все лучшие цены «своего» рынка (для удаления маржи против реального рынка).

    Для 1X2 — три исхода, для тоталов — over+under одной линии, для фор — обе стороны.
    """
    market, line = outcome.market, outcome.line
    prices: list[float] = []
    for other in outcomes:
        if other.market != market:
            continue
        if market in (Market.TOTALS, Market.HANDICAP):
            if other.line is None or line is None:
                continue
            if market == Market.TOTALS and abs(other.line - line) > 1e-6:
                continue
            # Форы задаются зеркально (Ф1 −1.5 и Ф2 +1.5) → сравниваем модули линий,
            # иначе обе стороны попадут в «разные рынки» и маржа не уберётся.
            if market == Market.HANDICAP and abs(abs(other.line) - abs(line)) > 1e-6:
                continue
        prices.append(other.best_price)
    return prices


# --------------------------------------------------------------------------- #
# Кандидаты и сигналы
# --------------------------------------------------------------------------- #
@dataclass
class ValueCandidate:
    market: str
    selection: str
    line: float | None
    odds: float
    odds_source: str
    prob_final: float
    prob_implied: float
    edge: float
    sources: int = 1
    implied_by_source: dict[str, float] = field(default_factory=dict)


def evaluate_outcome(
    outcome: AggregatedOutcome,
    probabilities: MarketProbabilities,
    all_outcomes: list[AggregatedOutcome],
    *,
    min_edge: float | None = None,
) -> ValueCandidate | None:
    """Считает edge для одного агрегированного исхода. None — если считать нечего."""
    if outcome.suspicious:
        return None
    prob_final = probabilities.probability_for(outcome.market, outcome.selection, outcome.line)
    if prob_final is None:
        return None
    market_prices = market_prices_for(outcome, all_outcomes) or [outcome.best_price]
    p_implied = implied_probability(outcome.best_price, market_prices)
    if p_implied <= 0:
        return None
    candidate_edge = edge(prob_final, p_implied)
    threshold = settings.value_threshold if min_edge is None else min_edge
    if candidate_edge < threshold:
        return None
    return ValueCandidate(
        market=outcome.market,
        selection=outcome.selection,
        line=outcome.line,
        odds=outcome.best_price,
        odds_source=outcome.best_source,
        prob_final=prob_final,
        prob_implied=p_implied,
        edge=candidate_edge,
        sources=outcome.sources,
        implied_by_source=dict(outcome.implied_by_source),
    )


def odds_in_range(odds: float) -> bool:
    return settings.odds_min <= odds <= settings.odds_max


def find_candidates(
    outcomes: list[AggregatedOutcome],
    probabilities: MarketProbabilities,
    *,
    is_live: bool = False,
) -> list[ValueCandidate]:
    """Все исходы, где edge ≥ порога и кэф в допустимом диапазоне."""
    min_edge = settings.value_threshold_live if is_live else settings.value_threshold
    candidates: list[ValueCandidate] = []
    for outcome in outcomes:
        if not odds_in_range(outcome.best_price):
            continue
        candidate = evaluate_outcome(outcome, probabilities, outcomes, min_edge=min_edge)
        if candidate:
            candidates.append(candidate)
    return candidates


def select_signals(
    candidates: list[ValueCandidate],
    *,
    confidence_score: float | None,
    data_quality: str,
    min_score: float | None = None,
    max_per_match: int | None = None,
    calibration: float | dict[str, float] = 1.0,
    is_live: bool = False,
) -> list[tuple[ValueCandidate, float]]:
    """Финальный отбор: score ≥ порога, data_quality ≠ weak, дедуп и лимит на матч.

    calibration может быть числом или словарём {market: коэффициент} — калибровка
    ведётся по связке (sport, league, market), см. learning/calibration.py.
    """
    from app.pipeline.confidence import composite_score  # локальный импорт против цикла

    if data_quality == DataQuality.WEAK:
        return []
    min_score = settings.confidence_min_score if min_score is None else min_score
    if max_per_match is None:
        max_per_match = settings.live_max_signals_per_match if is_live else settings.max_signals_per_match

    # score пересчитывается для каждого кандидата (edge у них разный)
    scored: list[tuple[ValueCandidate, float]] = []
    for candidate in candidates:
        market_calibration = (
            calibration.get(candidate.market, 1.0) if isinstance(calibration, dict) else calibration
        )
        score = composite_score(
            edge=candidate.edge,
            llm_confidence=confidence_score if confidence_score is not None else 0.5,
            data_quality=data_quality,
            calibration=market_calibration,
        )
        if score >= min_score:
            scored.append((candidate, score))

    scored.sort(key=lambda pair: pair[0].edge, reverse=True)
    selected: list[tuple[ValueCandidate, float]] = []
    seen: set[tuple[str, str, float | None]] = set()
    for candidate, score in scored:
        key = (candidate.market, candidate.selection, candidate.line)
        if key in seen:
            continue
        seen.add(key)
        selected.append((candidate, score))
        if len(selected) >= max_per_match:
            break
    return selected


def build_signal_payload(
    candidate: ValueCandidate,
    score: float,
    probabilities: MarketProbabilities,
    *,
    data_quality: str,
    is_live: bool = False,
) -> dict[str, Any]:
    """Готовит словарь для записи в signals."""
    stake = kelly_stake_pct(candidate.prob_final, candidate.odds)
    reasoning = probabilities.reasoning or "; ".join(probabilities.key_factors[:3]) or "ансамбль модели и LLM"
    return {
        "market": candidate.market,
        "selection": candidate.selection,
        "line": candidate.line,
        "odds": round(candidate.odds, 3),
        "odds_source": candidate.odds_source,
        "prob_final": round(candidate.prob_final, 4),
        "prob_implied": round(candidate.prob_implied, 4),
        "edge": round(candidate.edge, 4),
        "confidence_score": round(score, 1),
        "stake_pct": stake,
        "reasoning": reasoning,
        "key_factors": probabilities.key_factors[:5],
        "risk_notes": probabilities.risk_notes[:4],
        "data_quality": data_quality,
        "is_live": is_live,
        "sources": candidate.sources,
    }


def recompute_after_judge(
    probabilities: MarketProbabilities,
    outcome: AggregatedOutcome,
    all_outcomes: list[AggregatedOutcome],
    *,
    confidence_delta: float,
    is_live: bool = False,
) -> tuple[ValueCandidate | None, float]:
    """Пересчёт edge/score после корректировки арбитра (Модуль 4, вердикт adjust)."""
    from app.pipeline.confidence import composite_score

    candidate = evaluate_outcome(
        outcome, probabilities, all_outcomes, min_edge=0.0
    )
    if candidate is None:
        return None, 0.0
    score = composite_score(
        edge=candidate.edge,
        llm_confidence=probabilities.llm_confidence,
        data_quality=DataQuality.OK,
        calibration=1.0,
        confidence_delta=confidence_delta,
    )
    min_edge = settings.value_threshold_live if is_live else settings.value_threshold
    if candidate.edge < min_edge or score < settings.confidence_min_score or not odds_in_range(candidate.odds):
        return None, score
    return candidate, score


def round_odds(price: float) -> float:
    """Округление кэфа для отображения (не для расчётов!)."""
    return round(price / ODDS_ROUND_STEP) * ODDS_ROUND_STEP


def format_selection(market: str, selection: str, line: float | None) -> str:
    """Человекочитаемое название ставки для сообщения в Telegram."""
    names = {
        Selection.HOME: "П1",
        Selection.DRAW: "X",
        Selection.AWAY: "П2",
        Selection.DC_1X: "1X",
        Selection.DC_12: "12",
        Selection.DC_X2: "X2",
    }
    if market == Market.TOTALS and line is not None:
        return f"Т{'Б' if selection == Selection.OVER else 'М'} {line:g}"
    if market == Market.HANDICAP and line is not None:
        sign = "+" if line > 0 else ""
        return f"Ф{'1' if selection == Selection.HOME_HANDICAP else '2'} ({sign}{line:g})"
    if market == Market.DOUBLE_CHANCE:
        return f"Двойной шанс {names.get(selection, selection)}"
    return names.get(selection, selection)


def market_label(market: str) -> str:
    return {
        Market.ONE_X_TWO: "Исход",
        Market.TOTALS: "Тотал",
        Market.HANDICAP: "Фора",
        Market.DOUBLE_CHANCE: "Двойной шанс",
    }.get(market, market)


def sanity_log(candidates: list[ValueCandidate], match_id: int | str | None = None) -> None:
    if not candidates:
        logger.debug("value_engine: match_id={} — кандидатов нет", match_id)
        return
    best = max(candidates, key=lambda candidate: candidate.edge)
    logger.info(
        "value_engine: match_id={} — {} кандидатов, лучший: {} {} @ {} (edge {:+.2%})",
        match_id, len(candidates), best.market, best.selection, best.odds, best.edge,
    )
