"""Ур.2 — аналитик: полный контекст матча → вероятности (Модуль 4) + двухпроходный прематч (Модуль 6).

Что делает модуль:
  1. собирает контекст матча (кэфы агрегатора, форма, травмы, составы, H2H, календарь,
     новости, активные league_insights, стат-модель);
  2. прогоняет каскад: детерминированный скринер → дешёвая LLM → стат-модель →
     ANALYZER_MODEL (строгий JSON, pydantic, 1 ретрай) → ансамбль → Value Engine →
     арбитр (pipeline/judge.py) → запись сигналов;
  3. реализует двухпроходный прематч:
       PASS 1 (за 6 ч) — кандидаты со статусом candidate;
       PASS 2 (за 90 мин) — пере-анализ кандидатов со свежими составами/новостями,
                            финальный арбитраж и статус confirmed;
       матч, появившийся позже чем за 2 ч — один проход сразу;
  4. умеет работать в лайв-режиме (короткий контекст из live/esports_worker.py).

Честно про арбитра: Claude вызывается один раз — на финальном шаге перед рассылкой
(PASS 2 / live / single-pass), потому что каждый вызов стоит денег. Кандидаты PASS 1
сохраняются без арбитра — это осознанное решение, а не забытый шаг.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from loguru import logger
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import and_, desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import (
    Analysis,
    DataQuality,
    EnsembleWeight,
    LeagueCalibration,
    LeagueInsight,
    Match,
    MatchStatus,
    Signal,
    SignalStatus,
    Sport,
    StatPrediction as StatPredictionRow,
)
from app.llm_client import LLMError, get_llm_client
from app.pipeline import screener
from app.pipeline.confidence import composite_score
from app.pipeline.ensemble import EnsembleWeights, ensemble
from app.pipeline.judge import judge_signal
from app.pipeline.llm_prompts import analyzer_system, build_analyzer_user_prompt
from app.pipeline.llm_screener import llm_screen
from app.pipeline.value_engine import (
    MarketProbabilities,
    build_signal_payload,
    find_candidates,
    market_prices_for,
    odds_in_range,
    select_signals,
)
from app.stats_models.base import StatPrediction
from app.stats_models.basketball import BasketballModel
from app.stats_models.elo import EloModel, get_elo
from app.stats_models.poisson import PoissonModel

# --------------------------------------------------------------------------- #
# Модель ответа аналитика (Ур.2)
# --------------------------------------------------------------------------- #
class AnalysisResult(BaseModel):
    prob_home: float
    prob_draw: float | None = None
    prob_away: float
    expected_total: float
    total_sigma: float
    expected_margin: float
    margin_sigma: float
    confidence: float
    data_quality: Literal["ok", "weak"] = "ok"
    key_factors: list[str] = Field(default_factory=list)
    risk_notes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> AnalysisResult:
        total = self.prob_home + (self.prob_draw or 0.0) + self.prob_away
        if abs(total - 1.0) > 0.02:
            # ТЗ: расхождение суммы > 0.02 → ретрай (его делает клиент по ValidationError)
            # + штраф к confidence, если ответ всё же принят.
            raise ValueError(f"сумма вероятностей {total:.4f} отличается от 1 более чем на 0.02")
        if abs(total - 1.0) > 1e-6:
            self.confidence = max(0.0, self.confidence - 0.05 * abs(total - 1.0) / 0.02)
            self.risk_notes.append("вероятности не были нормированы моделью (штраф к confidence)")
            self.prob_home /= total
            if self.prob_draw is not None:
                self.prob_draw /= total
            self.prob_away /= total
        self.confidence = min(max(self.confidence, 0.0), 1.0)
        if self.total_sigma <= 0 or self.margin_sigma <= 0:
            raise ValueError("sigma должна быть > 0")
        return self


@dataclass
class AnalysisReport:
    """Результат прогона одного матча (для логов и планировщика)."""

    match_id: int
    status: str                       # skipped | screener_skip | llm_skip | no_candidates | signals | error
    reason: str = ""
    candidates: int = 0
    signals: int = 0
    confirmed: int = 0
    rejected: int = 0


# --------------------------------------------------------------------------- #
# Сбор контекста
# --------------------------------------------------------------------------- #
def _match_summary(match: Match, sport_code: str, inputs: screener.ScreenInput) -> dict[str, Any]:
    return {
        "match_id": match.id,
        "sport": sport_code,
        "league": match.league,
        "teams": f"{getattr(match, '_home_name', '')} — {getattr(match, '_away_name', '')}",
        "starts_at": match.starts_at.isoformat() if match.starts_at else None,
        **screener.summary(inputs),
    }


async def _team_names(session: AsyncSession, match: Match) -> tuple[str, str]:
    """Достаёт канонические имена команд (для промптов и матчинга статистики)."""
    from app.db.models import Team

    home = away = None
    if match.home_team_id:
        home = (await session.execute(select(Team.canonical_name).where(Team.id == match.home_team_id))).scalar_one_or_none()
    if match.away_team_id:
        away = (await session.execute(select(Team.canonical_name).where(Team.id == match.away_team_id))).scalar_one_or_none()
    return home or "", away or ""


async def _analyst_probabilities(
    match: Match,
    home_name: str,
    away_name: str,
    analysis: AnalysisResult,
    stat_prediction: StatPrediction | None,
    weights: EnsembleWeights,
) -> tuple[MarketProbabilities, dict[str, Any]]:
    """Считает ансамбль и переводит его в MarketProbabilities для Value Engine."""
    result = ensemble(
        llm_prob_home=analysis.prob_home,
        llm_prob_away=analysis.prob_away,
        llm_prob_draw=analysis.prob_draw,
        llm_expected_total=analysis.expected_total,
        llm_total_sigma=analysis.total_sigma,
        llm_expected_margin=analysis.expected_margin,
        llm_margin_sigma=analysis.margin_sigma,
        stat=stat_prediction,
        weights=weights,
    )
    probabilities = MarketProbabilities(
        prob_home=result.prob_home,
        prob_away=result.prob_away,
        prob_draw=result.prob_draw,
        expected_total=result.expected_total,
        total_sigma=result.total_sigma,
        expected_margin=result.expected_margin,
        margin_sigma=result.margin_sigma,
        llm_confidence=analysis.confidence,
        key_factors=analysis.key_factors,
        risk_notes=analysis.risk_notes + result.notes,
        reasoning="; ".join(analysis.key_factors[:3]),
        source="ensemble" if result.used_stat_model else "llm",
    )
    analytics = {
        "ensemble": result.as_dict(),
        "llm": {
            "prob_home": analysis.prob_home,
            "prob_draw": analysis.prob_draw,
            "prob_away": analysis.prob_away,
            "expected_total": analysis.expected_total,
            "total_sigma": analysis.total_sigma,
            "expected_margin": analysis.expected_margin,
            "margin_sigma": analysis.margin_sigma,
            "confidence": analysis.confidence,
            "data_quality": analysis.data_quality,
            "key_factors": analysis.key_factors,
            "risk_notes": analysis.risk_notes,
        },
        "stat": stat_prediction.to_prompt_json() if stat_prediction else None,
    }
    return probabilities, analytics


def _data_quality(analysis: AnalysisResult, stat_prediction: StatPrediction | None) -> str:
    if analysis.data_quality == "weak":
        return DataQuality.WEAK
    if stat_prediction is not None and stat_prediction.data_quality == "weak":
        # Стат-модель на слабых данных — не блокируем сигнал, но ужесточаем требования:
        # считаем как ok, если аналитик дал ok (LLM видит больше контекста).
        return DataQuality.OK
    return DataQuality.OK


async def _league_insights(session: AsyncSession, sport_code: str, league: str) -> dict[str, Any] | None:
    row = (
        await session.execute(
            select(LeagueInsight)
            .where(LeagueInsight.sport == sport_code, LeagueInsight.league == league, LeagueInsight.active.is_(True))
            .order_by(desc(LeagueInsight.created_at))
            .limit(1)
        )
    ).scalar_one_or_none()
    return row.insights_json if row else None


async def _ensemble_weights(session: AsyncSession, sport_code: str) -> EnsembleWeights:
    row = (
        await session.execute(select(EnsembleWeight).where(EnsembleWeight.sport == sport_code))
    ).scalar_one_or_none()
    if row is None:
        return EnsembleWeights()
    return EnsembleWeights(w_llm=row.w_llm, w_stat=row.w_stat)


async def _calibrations(session: AsyncSession, sport_code: str, league: str) -> dict[str, float]:
    rows = (
        await session.execute(
            select(LeagueCalibration.market, LeagueCalibration.calibration).where(
                LeagueCalibration.sport == sport_code, LeagueCalibration.league == league
            )
        )
    ).all()
    return {market: value for market, value in rows}


async def _already_analyzed(session: AsyncSession, match_id: int, pass_no: int) -> bool:
    row = (
        await session.execute(
            select(Analysis.id)
            .where(Analysis.match_id == match_id, Analysis.pass_no == pass_no, Analysis.level == 2)
            .order_by(desc(Analysis.created_at))
            .limit(1)
        )
    ).scalar_one_or_none()
    return row is not None


# --------------------------------------------------------------------------- #
# Статистические прогнозы по спортам
# --------------------------------------------------------------------------- #
async def build_stat_prediction(
    session: AsyncSession, match: Match, sport_code: str, providers: Any, home_name: str, away_name: str
) -> StatPrediction | None:
    """Считает стат-модель для конкретного спорта. Любая ошибка → None (деградация)."""
    try:
        if sport_code == "football":
            return await _football_prediction(session, match, providers, home_name, away_name)
        if sport_code == "hockey":
            return await _hockey_prediction(session, match, providers, home_name, away_name)
        if sport_code == "basketball":
            return await _basketball_prediction(providers, match, home_name, away_name)
        if sport_code in ("dota2", "cs2"):
            return await _esports_prediction(session, match, sport_code, home_name, away_name)
    except Exception as exc:  # noqa: BLE001 — модель не должна ронять пайплайн
        logger.exception("analyzer: стат-модель для match_id={} упала: {}", match.id, exc)
    return None


async def _football_prediction(
    session: AsyncSession, match: Match, providers: Any, home_name: str, away_name: str
) -> StatPrediction | None:
    from app.sources.understat import league_code_for

    league_code = league_code_for(match.league)
    if not league_code:
        logger.debug("analyzer: лига «{}» не маппится на Understat — футбольная модель пропущена", match.league)
        return None
    understat = providers.understat
    home_stats = away_stats = None
    if match.home_team_id:
        home_stats = await understat.get_team_xg(session, match.home_team_id, home_name, league_code)
    if match.away_team_id:
        away_stats = await understat.get_team_xg(session, match.away_team_id, away_name, league_code)
    if home_stats is None or away_stats is None:
        # Пробуем сопоставить по названиям напрямую (для не-русских источников кэфов).
        league = await understat.fetch_league(league_code)
        home_stats = home_stats or understat._match_team(league, home_name)
        away_stats = away_stats or understat._match_team(league, away_name)
    if home_stats is None or away_stats is None:
        logger.warning(
            "analyzer: match_id={} — не удалось получить xG для «{}» / «{}» (лига {})",
            match.id, home_name, away_name, league_code,
        )
        return None
    league_home, league_away = await understat.get_league_averages(league_code)
    model = PoissonModel(sport="football")
    prediction = await model.predict(home_stats, away_stats, league_home, league_away)
    if prediction:
        await _store_stat_prediction(session, match.id, prediction)
    return prediction


async def _hockey_prediction(
    session: AsyncSession, match: Match, providers: Any, home_name: str, away_name: str
) -> StatPrediction | None:
    """Хоккей: xGoals MoneyPuck (если команда сопоставилась) + домашнее преимущество.

    ⚠️ MoneyPuck хранит 3-буквенные аббревиатуры; названия команд у нас приходят из
    букмекера (часто по-русски). Сопоставляем через NHL-индекс аббревиатур; если не
    получилось — модель падает на голы NHL API (тоже Пуассон, но без xG) — это
    осознанная деградация, о которой пишет лог.
    """
    nhl = providers.nhl
    home_abbrev = await nhl.resolve_abbrev(home_name)
    away_abbrev = await nhl.resolve_abbrev(away_name)

    home_xg = await providers.moneypuck.get_team_xg(home_abbrev or home_name)
    away_xg = await providers.moneypuck.get_team_xg(away_abbrev or away_name)

    if home_xg is None or away_xg is None:
        # Фоллбэк: голы за матч из NHL API → синтетические xG-профили.
        home_gf, home_ga = await nhl.get_team_goals_per_game(home_name)
        away_gf, away_ga = await nhl.get_team_goals_per_game(away_name)
        if None in (home_gf, home_ga, away_gf, away_ga):
            logger.warning(
                "analyzer: match_id={} — нет хоккейных данных для «{}» / «{}»", match.id, home_name, away_name
            )
            return None
        from app.sources.base import TeamXgStats

        home_xg = TeamXgStats(team=home_name, matches=0, xg_for_per_game=home_gf, xg_against_per_game=home_ga, source="nhl:голы")
        away_xg = TeamXgStats(team=away_name, matches=0, xg_for_per_game=away_gf, xg_against_per_game=away_ga, source="nhl:голы")

    model = PoissonModel(sport="hockey")
    prediction = await model.predict(home_xg, away_xg, home_advantage=settings.hockey_home_advantage)
    if prediction:
        await _store_stat_prediction(session, match.id, prediction)
    return prediction


async def _basketball_prediction(providers: Any, match: Match, home_name: str, away_name: str) -> StatPrediction | None:
    league_key = "nba" if "nba" in match.league.lower() else ("euroleague" if "евролиг" in match.league.lower() else "default")
    home_stats = await providers.basketball.get_team_stats(home_name, league_key=league_key)
    away_stats = await providers.basketball.get_team_stats(away_name, league_key=league_key)
    if (not home_stats or home_stats.ortg is None) and providers.balldontlie.available:
        home_stats = await providers.balldontlie.get_team_stats(home_name)
        away_stats = await providers.balldontlie.get_team_stats(away_name)
    model = BasketballModel()
    return await model.predict(home_stats, away_stats, league_key=league_key)


async def _esports_prediction(
    session: AsyncSession, match: Match, sport_code: str, home_name: str, away_name: str
) -> StatPrediction | None:
    if not match.home_team_id or not match.away_team_id:
        return None
    sport_id = (await session.execute(select(Sport.id).where(Sport.code == sport_code))).scalar_one_or_none()
    if sport_id is None:
        return None
    elo_home, matches_home = await get_elo(session, sport_id, match.home_team_id, None)
    elo_away, matches_away = await get_elo(session, sport_id, match.away_team_id, None)
    model = EloModel()
    return await model.predict(elo_home, elo_away, matches_home=matches_home, matches_away=matches_away)


async def _store_stat_prediction(session: AsyncSession, match_id: int, prediction: StatPrediction) -> None:
    session.add(
        StatPredictionRow(
            match_id=match_id,
            model=prediction.model,
            parsed=prediction.model_dump(mode="json"),
        )
    )
    await session.flush()


# --------------------------------------------------------------------------- #
# Основной прогон матча
# --------------------------------------------------------------------------- #
async def analyze_match(
    session: AsyncSession,
    match: Match,
    *,
    pass_no: int,
    providers: Any,
    is_live: bool = False,
    live_context: dict[str, Any] | None = None,
    run_judge: bool = True,
    force: bool = False,
) -> AnalysisReport:
    """Полный прогон одного матча по каскаду."""
    sport_code = (
        (await session.execute(select(Sport.code).where(Sport.id == match.sport_id))).scalar_one_or_none() or "unknown"
    )
    home_name, away_name = await _team_names(session, match)
    log = logger.bind(match_id=match.id, sport=sport_code, pass_no=pass_no, live=is_live)

    if not force and pass_no is not None and await _already_analyzed(session, match.id, pass_no):
        return AnalysisReport(match.id, "skipped", "матч уже анализировался в этом проходе")

    # ---------------------------------------------------------------- кэфы
    outcomes = await providers.aggregator.refresh_match(session, match, live=is_live)
    if not outcomes:
        log.warning("analyzer: нет кэфов по матчу — пропуск")
        return AnalysisReport(match.id, "skipped", "нет кэфов")

    # ------------------------------------------------- Ур.0 — скринер (код)
    stats_ready = await _stats_available(providers, sport_code, match, home_name, away_name, is_live)
    screen_input = screener.ScreenInput(
        sport=sport_code,
        league=match.league,
        league_tier=match.league_tier,
        outcomes=outcomes,
        has_stats=stats_ready,
        is_esports=sport_code in ("dota2", "cs2"),
        starts_at=match.starts_at,
    )
    screen_result = screener.screen(screen_input)
    if not screen_result.passed:
        log.info("analyzer: Ур.0 отклонил — {}", "; ".join(screen_result.reasons))
        return AnalysisReport(match.id, "screener_skip", "; ".join(screen_result.reasons))
    if screen_result.data_quality == DataQuality.WEAK:
        match.data_quality = DataQuality.WEAK
    log.info("analyzer: Ур.0 пройден ({})", "; ".join(screen_result.checked) or "ок")

    # ---------------------------------------- Ур.1 — дешёвый LLM-скринер
    summary = screener.summary(screen_input)
    summary["teams"] = {"home": home_name, "away": away_name}
    verdict = await llm_screen(summary, match.id)
    session.add(
        Analysis(
            match_id=match.id,
            model=settings.screener_model,
            level=1,
            pass_no=pass_no,
            raw_json=json.dumps(summary, ensure_ascii=False)[:8000],
            parsed=verdict.model_dump(),
        )
    )
    await session.flush()
    if verdict.verdict == "skip":
        log.info("analyzer: Ур.1 отклонил — {}", verdict.reason[:200])
        return AnalysisReport(match.id, "llm_skip", verdict.reason)

    # ------------------------------------------- Ур.2 — стат-модель (код)
    stat_prediction = await build_stat_prediction(session, match, sport_code, providers, home_name, away_name)
    if stat_prediction is None:
        log.info("analyzer: стат-модель недоступна — работаем на LLM (по ТЗ это норма для тенниса/бокса/MMA)")

    # --------------------------------------- Ур.2 — аналитик (LLM, полный)
    context = await build_context(
        session, match, sport_code, outcomes, stat_prediction, providers,
        home_name=home_name, away_name=away_name, is_live=is_live, live_context=live_context,
    )
    analysis = await _run_analyst_llm(context, match.id, is_live=is_live, pass_no=pass_no)
    if analysis is None:
        return AnalysisReport(match.id, "error", "аналитик не вернул валидный ответ")

    # ----------------------------------------------------- ансамбль и value
    weights = await _ensemble_weights(session, sport_code)
    probabilities, analytics = await _analyst_probabilities(
        match, home_name, away_name, analysis, stat_prediction, weights
    )
    context["analyst_probabilities"] = analytics["llm"]
    context["llm_confidence"] = analysis.confidence

    candidates = find_candidates(outcomes, probabilities, is_live=is_live)
    log.info(
        "analyzer: кандидатов value {} (лучший edge {})",
        len(candidates), f"{max((c.edge for c in candidates), default=0):+.2%}",
    )
    if not candidates:
        return AnalysisReport(match.id, "no_candidates", "edge ниже порога")

    data_quality = _data_quality(analysis, stat_prediction)
    calibrations = await _calibrations(session, sport_code, match.league)
    selected = select_signals(
        candidates,
        confidence_score=analysis.confidence,
        data_quality=data_quality,
        calibration=calibrations,
        is_live=is_live,
    )
    if not selected:
        log.info("analyzer: сигналов нет — не прошли score/калибровку/лимит")
        return AnalysisReport(match.id, "no_candidates", "score ниже порога", candidates=len(candidates))

    # --------------------------------------------- запись + арбитр + статус
    confirmed = rejected = 0
    for candidate, score in selected:
        payload = build_signal_payload(
            candidate,
            score,
            probabilities,
            data_quality=data_quality,
            sport=match.sport.code if getattr(match, "sport", None) else None,
            is_live=is_live,
        )
        signal = Signal(
            match_id=match.id,
            market=payload["market"],
            selection=payload["selection"],
            line=payload["line"],
            odds=payload["odds"],
            odds_source=payload["odds_source"],
            prob_final=payload["prob_final"],
            prob_implied=payload["prob_implied"],
            edge=payload["edge"],
            confidence_score=payload["confidence_score"],
            stake_pct=payload["stake_pct"],
            reasoning=payload["reasoning"],
            key_factors=payload["key_factors"],
            risk_notes=payload["risk_notes"],
            status=SignalStatus.CANDIDATE,
            is_live=is_live,
            live_stage=(live_context or {}).get("stage"),
            pass_no=pass_no,
        )
        session.add(signal)
        await session.flush()
        log.info(
            "analyzer: сигнал candidate id={} {} {} @ {} (edge {:+.2%}, score {})",
            signal.id, signal.market, signal.selection, signal.odds, signal.edge, signal.confidence_score,
        )
        if run_judge:
            status, _verdict, _score = await judge_signal(
                session, signal, context, stat_prediction, outcomes, is_live=is_live
            )
            if status == SignalStatus.CONFIRMED:
                confirmed += 1
            else:
                rejected += 1
    return AnalysisReport(
        match.id, "signals", "ок",
        candidates=len(candidates), signals=len(selected), confirmed=confirmed, rejected=rejected,
    )


async def _stats_available(
    providers: Any, sport_code: str, match: Match, home_name: str, away_name: str, is_live: bool
) -> bool:
    """Есть ли「статистика доступна」 для Ур.0 (мин. 5 матчей или подтверждённый контекст)."""
    if is_live:
        return True
    try:
        if sport_code == "football" and providers.football.available:
            home_form = await providers.football.get_recent_form(home_name, n=settings.min_recent_matches)
            away_form = await providers.football.get_recent_form(away_name, n=settings.min_recent_matches)
            return min(len(home_form), len(away_form)) >= settings.min_recent_matches
        if sport_code == "hockey":
            home_form = await providers.nhl.get_recent_form(home_name, n=5)
            away_form = await providers.nhl.get_recent_form(away_name, n=5)
            return min(len(home_form), len(away_form)) >= 3
        if sport_code == "basketball":
            home_games = await providers.basketball.get_games()
            return bool(home_games)
        if sport_code in ("tennis", "mma", "boxing"):
            return await _has_context(providers, sport_code, home_name, away_name)
    except Exception as exc:  # noqa: BLE001
        logger.warning("analyzer: проверка статистики упала для match_id={}: {}", match.id, exc)
        return False
    return False


async def _has_context(providers: Any, sport_code: str, home_name: str, away_name: str) -> bool:
    """Для видов без стат-модели «данными» считаем форму/H2H из источника или новости."""
    try:
        if sport_code == "tennis":
            form = await providers.tennis.get_recent_form(home_name, n=3)
            return len(form) > 0
        if sport_code in ("mma", "boxing"):
            news = await providers.mma.get_news(query=home_name, limit=3, hours_back=24 * 30)
            return bool(news)
    except Exception:  # noqa: BLE001
        return False
    return False


async def _run_analyst_llm(
    context: dict[str, Any], match_id: int, *, is_live: bool, pass_no: int
) -> AnalysisResult | None:
    client = get_llm_client()
    if not client.available:
        logger.error("analyzer: OPENROUTER_API_KEY не задан — уровень 2 невозможен")
        return None
    sport = context.get("match", {}).get("sport", "football")
    try:
        analysis, raw = await client.complete_model(
            AnalysisResult,
            model=settings.analyzer_model,
            system_prompt=analyzer_system(sport),
            user_prompt=build_analyzer_user_prompt(context),
            max_tokens=settings.llm_max_tokens_analyzer,
            temperature=settings.llm_temperature_analyzer,
            timeout=settings.llm_analyzer_timeout_sec,
            label="analyzer",
            match_id=match_id,
        )
    except LLMError as exc:
        logger.warning("analyzer: match_id={} — аналитик не ответил: {}", match_id, exc)
        return None
    # Сохраняем «журнал» ответа (см. analyses): raw + parsed
    context["_analysis_raw"] = raw
    logger.info(
        "analyzer: match_id={} — LLM: p({:.2f}/{}/{:.2f}), total {:.2f}±{:.2f}, conf {:.2f}, {}/{}",
        match_id, analysis.prob_home,
        f"{analysis.prob_draw:.2f}" if analysis.prob_draw is not None else "—",
        analysis.prob_away, analysis.expected_total, analysis.total_sigma, analysis.confidence,
        analysis.data_quality, pass_no,
    )
    return analysis


async def build_context(
    session: AsyncSession,
    match: Match,
    sport_code: str,
    outcomes: list[Any],
    stat_prediction: StatPrediction | None,
    providers: Any,
    *,
    home_name: str,
    away_name: str,
    is_live: bool = False,
    live_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Собирает полный контекст матча для промпта аналитика."""
    odds_lines: list[dict[str, Any]] = []
    for outcome in sorted(outcomes, key=lambda o: (o.market, o.selection, o.line or 0)):
        if not odds_in_range(outcome.best_price) and outcome.market not in ("1x2",):
            continue
        odds_lines.append(
            {
                "market": outcome.market,
                "selection": outcome.selection,
                "line": outcome.line,
                "best_price": round(outcome.best_price, 3),
                "best_source": outcome.best_source,
                "implied": round(outcome.implied, 4),
                "sources": outcome.sources,
                "suspicious": outcome.suspicious,
            }
        )

    context: dict[str, Any] = {
        "match": {
            "match_id": match.id,
            "sport": sport_code,
            "league": match.league,
            "league_tier": match.league_tier,
            "home_team": home_name,
            "away_team": away_name,
            "starts_at": match.starts_at.isoformat() if match.starts_at else None,
            "status": match.status,
            "live_stage": match.live_stage,
            "data_quality": match.data_quality,
        },
        "odds_lines": odds_lines,
        "suspicious_lines": [
            {"market": o.market, "selection": o.selection, "line": o.line, "reason": o.suspicious_reason}
            for o in outcomes
            if o.suspicious
        ],
        "stat_model": stat_prediction.to_prompt_json() if stat_prediction else None,
        "league_insights": await _league_insights(session, sport_code, match.league),
    }

    try:
        context.update(
            await _context_stats(
                providers, sport_code, match, home_name, away_name, is_live=is_live, live_context=live_context
            )
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("analyzer: сбор статистики для match_id={} частично не удался: {}", match.id, exc)
    return context


async def _context_stats(
    providers: Any,
    sport_code: str,
    match: Match,
    home_name: str,
    away_name: str,
    *,
    is_live: bool,
    live_context: dict[str, Any] | None,
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if is_live and live_context:
        out["live_info"] = live_context
        out["odds_movement"] = live_context.get("odds_movement")
        return out

    if sport_code == "football":
        football = providers.football
        if football.available:
            out["form"] = {
                "home": [entry.model_dump(mode="json") for entry in await football.get_recent_form(home_name, n=5)],
                "away": [entry.model_dump(mode="json") for entry in await football.get_recent_form(away_name, n=5)],
            }
            out["injuries"] = [
                injury.model_dump(mode="json") for injury in await football.get_injuries(home_name)
            ] + [injury.model_dump(mode="json") for injury in await football.get_injuries(away_name)]
            out["h2h"] = [record.model_dump(mode="json") for record in await football.get_h2h(home_name, away_name)]
            out["schedule_density"] = {
                "home": (await football.get_schedule_density(home_name)).model_dump(mode="json"),
                "away": (await football.get_schedule_density(away_name)).model_dump(mode="json"),
            }
            lineups = {}
            for name in (home_name, away_name):
                lineup = await football.get_predicted_lineups(name)
                if lineup:
                    lineups[name] = lineup.model_dump(mode="json")
            if lineups:
                out["lineups"] = lineups
    elif sport_code == "hockey":
        nhl = providers.nhl
        out["form"] = {
            "home": [entry.model_dump(mode="json") for entry in await nhl.get_recent_form(home_name, n=6)],
            "away": [entry.model_dump(mode="json") for entry in await nhl.get_recent_form(away_name, n=6)],
        }
        out["lineups"] = {
            "probable_goalies": {
                "home": await nhl.get_probable_goalie(home_name),
                "away": await nhl.get_probable_goalie(away_name),
            },
            "note": "вероятный вратарь — эвристика по фактическим стартам (см. sources/nhl.py)",
        }
        out["schedule_density"] = {
            "home": (await nhl.get_schedule_density(home_name)).model_dump(mode="json"),
            "away": (await nhl.get_schedule_density(away_name)).model_dump(mode="json"),
        }
        out["h2h"] = [record.model_dump(mode="json") for record in await nhl.get_h2h(home_name, away_name)]
    elif sport_code == "basketball":
        provider = providers.basketball
        out["form"] = {
            "home": [entry.model_dump(mode="json") for entry in await provider.get_recent_form(home_name, n=6)],
            "away": [entry.model_dump(mode="json") for entry in await provider.get_recent_form(away_name, n=6)],
        }
        out["schedule_density"] = {
            "home": (await provider.get_schedule_density(home_name)).model_dump(mode="json"),
            "away": (await provider.get_schedule_density(away_name)).model_dump(mode="json"),
        }
        out["h2h"] = [record.model_dump(mode="json") for record in await provider.get_h2h(home_name, away_name)][:8]
    elif sport_code == "tennis":
        tennis = providers.tennis
        out["form"] = {
            "home": [entry.model_dump(mode="json") for entry in await tennis.get_recent_form(home_name, n=6)],
            "away": [entry.model_dump(mode="json") for entry in await tennis.get_recent_form(away_name, n=6)],
        }
        out["h2h"] = [record.model_dump(mode="json") for record in await tennis.get_h2h(home_name, away_name)]
        out["schedule_density"] = {
            "home": (await tennis.get_schedule_density(home_name)).model_dump(mode="json"),
            "away": (await tennis.get_schedule_density(away_name)).model_dump(mode="json"),
        }
    elif sport_code in ("mma", "boxing"):
        out["news"] = await providers.mma.get_news_context([home_name, away_name], limit=10)
        out["injuries"] = [
            injury.model_dump(mode="json")
            for injury in (await providers.mma.get_injuries(home_name))[:4]
        ]
    elif sport_code in ("dota2", "cs2"):
        liquipedia = getattr(providers, "liquipedia", None)
        if liquipedia and liquipedia.available:
            esports: dict[str, Any] = {}
            for key, call in (
                ("tier", lambda: liquipedia.get_tournament_tier(match.league)),
                ("roster_home", lambda: liquipedia.get_team_roster(sport_code, home_name)),
                ("roster_away", lambda: liquipedia.get_team_roster(sport_code, away_name)),
            ):
                try:
                    esports[key] = await call()
                except Exception as exc:  # noqa: BLE001 — 404 / открытый Circuit Breaker не валят анализ
                    logger.warning("analyzer: Liquipedia {} недоступна для match_id={}: {}", key, match.id, exc)
                    esports[key] = None
            out["esports"] = esports
    return out


# --------------------------------------------------------------------------- #
# Двухпроходный прематч (Модуль 6)
# --------------------------------------------------------------------------- #
def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _matches_in_window(session: AsyncSession, center: datetime, window_minutes: float) -> list[Match]:
    window = timedelta(minutes=window_minutes)
    rows = (
        await session.execute(
            select(Match)
            .where(
                Match.status == MatchStatus.SCHEDULED,
                Match.starts_at >= center - window,
                Match.starts_at <= center + window,
            )
            .order_by(Match.starts_at)
        )
    ).scalars().all()
    return list(rows)


async def run_prematch_pass(session: AsyncSession, pass_no: int, providers: Any = None) -> dict[str, Any]:
    """Один проход по расписанию: PASS 1 (T−6ч) или PASS 2 (T−90м).

    Возвращает сводку для лога/планировщика.
    """
    if providers is None:
        from app.pipeline.collector import get_providers

        providers = get_providers()

    threshold = (
        _now() + timedelta(hours=settings.pass1_hours_before)
        if pass_no == 1
        else _now() + timedelta(minutes=settings.pass2_minutes_before)
    )
    matches = await _matches_in_window(session, threshold, settings.pass_window_minutes)
    logger.info("analyzer: PASS {} — матчей в окне: {}", pass_no, len(matches))

    stats = {"pass": pass_no, "matches": len(matches), "signals": 0, "confirmed": 0, "rejected": 0, "errors": 0}
    for match in matches:
        if stats["signals"] >= settings.pass1_max_matches:
            logger.warning("analyzer: защитный лимит PASS {} исчерпан ({})", pass_no, settings.pass1_max_matches)
            break
        try:
            report = await analyze_match(
                session, match, pass_no=pass_no, providers=providers, run_judge=(pass_no == 2)
            )
            stats["signals"] += report.signals
            stats["confirmed"] += report.confirmed
            stats["rejected"] += report.rejected
            if report.status == "error":
                stats["errors"] += 1
        except Exception as exc:  # noqa: BLE001 — один матч не должен валить проход
            stats["errors"] += 1
            logger.exception("analyzer: PASS {} — ошибка на match_id={}: {}", pass_no, match.id, exc)
        await session.commit()
    if pass_no == 2:
        stats["stale_rejected"] = await reject_stale_candidates(session, [match.id for match in matches])
    logger.info("analyzer: PASS {} завершён: {}", pass_no, stats)
    return stats


# Матч, стартующий в пределах этого окна, уже не успеет пройти PASS 2 — кандидат устарел.
STALE_CUTOFF_MINUTES = 5


async def reject_stale_candidates(session: AsyncSession, match_ids: list[int] | None = None) -> int:
    """Закрывает устаревших кандидатов.

    • match_ids передан — кандидаты PASS 1 этих матчей, не подтвердившиеся на PASS 2;
    • match_ids is None — все CANDIDATE, чей матч уже начался или стартует в ближайшие
      STALE_CUTOFF_MINUTES (Match.starts_at <= cutoff).
    """
    query = select(Signal).where(Signal.status == SignalStatus.CANDIDATE)
    if match_ids is None:
        cutoff = _now() + timedelta(minutes=STALE_CUTOFF_MINUTES)
        query = query.join(Match, Match.id == Signal.match_id).where(Match.starts_at <= cutoff)
    elif not match_ids:
        return 0
    else:
        query = query.where(Signal.match_id.in_(match_ids))
    rows = (await session.execute(query)).scalars().all()
    for signal in rows:
        signal.status = SignalStatus.REJECTED
        signal.judge_reason = "PASS 2: edge не подтверждён свежими данными (составы/линия изменились)"
        logger.info("analyzer: кандидат signal_id={} отклонён на PASS 2", signal.id)
    if rows:
        await session.flush()
    return len(rows)


async def run_late_matches(session: AsyncSession, providers: Any = None) -> dict[str, Any]:
    """Матчи, появившиеся позже чем за 2 часа до старта — один проход сразу (с арбитром)."""
    if providers is None:
        from app.pipeline.collector import get_providers

        providers = get_providers()
    now = _now()
    matches = (
        await session.execute(
            select(Match)
            .where(
                Match.status == MatchStatus.SCHEDULED,
                Match.starts_at >= now,
                Match.starts_at <= now + timedelta(hours=settings.late_match_hours),
            )
            .order_by(Match.starts_at)
        )
    ).scalars().all()

    stats = {"late_matches": len(matches), "signals": 0, "confirmed": 0}
    for match in matches:
        if await _has_candidate(session, match.id) or await _already_analyzed(session, match.id, 2):
            continue
        report = await analyze_match(session, match, pass_no=2, providers=providers, run_judge=True, force=True)
        stats["signals"] += report.signals
        stats["confirmed"] += report.confirmed
        await session.commit()
    if stats["late_matches"]:
        logger.info("analyzer: поздние матчи обработаны одним проходом: {}", stats)
    return stats


async def _has_candidate(session: AsyncSession, match_id: int) -> bool:
    row = (
        await session.execute(
            select(Signal.id).where(
                Signal.match_id == match_id,
                Signal.status.in_((SignalStatus.CANDIDATE, SignalStatus.CONFIRMED, SignalStatus.SENT)),
            ).limit(1)
        )
    ).scalar_one_or_none()
    return row is not None


async def analyze_live_candidate(
    session: AsyncSession,
    match: Match,
    live_context: dict[str, Any],
    *,
    providers: Any = None,
    stat_override: StatPrediction | None = None,
) -> AnalysisReport:
    """Лайв-прогон (Модуль 7): короткий пакет → аналитик → value → арбитр (сокращённый промпт)."""
    if providers is None:
        from app.pipeline.collector import get_providers

        providers = get_providers()
    return await analyze_match(
        session,
        match,
        pass_no=2,
        providers=providers,
        is_live=True,
        live_context=live_context,
        run_judge=True,
        force=True,
    )
