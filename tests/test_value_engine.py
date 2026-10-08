"""Юнит-тесты Value Engine: implied, edge, тоталы, форы, двойной шанс, Келли."""

from __future__ import annotations

import statistics

import pytest

from app.config import settings
from app.db.models import DataQuality
from app.pipeline.confidence import composite_score
from app.pipeline.value_engine import (
    MarketProbabilities as MP,
)
from app.pipeline.value_engine import (
    ValueCandidate,
    build_signal_payload,
    evaluate_outcome,
    find_candidates,
    format_selection,
    implied_probabilities,
    implied_probability,
    kelly_stake_pct,
    market_prices_for,
    odds_in_range,
    select_signals,
)
from app.sources.odds_aggregator import AggregatedOutcome


def outcome(
    market: str, selection: str, price: float, line: float | None = None, suspicious: bool = False
) -> AggregatedOutcome:
    return AggregatedOutcome(
        market=market,
        selection=selection,
        line=line,
        best_price=price,
        best_source="winline",
        implied=1.0 / price,
        implied_by_source={"winline": 1.0 / price},
        sources=1,
        suspicious=suspicious,
    )


# --------------------------------------------------------------------------- #
# Implied probabilities
# --------------------------------------------------------------------------- #
def test_implied_probabilities_normalized():
    probs = implied_probabilities([2.0, 3.5, 4.0])
    assert sum(probs) == pytest.approx(1.0, abs=1e-9)
    # 1/2 = 0.5, 1/3.5 = 0.285714, 1/4 = 0.25 → сумма 1.035714
    assert probs[0] == pytest.approx(0.5 / 1.035714, abs=1e-6)
    assert probs[1] == pytest.approx(0.285714 / 1.035714, abs=1e-6)
    assert probs[2] == pytest.approx(0.25 / 1.035714, abs=1e-6)


def test_implied_probability_single_price():
    assert implied_probability(2.0, [2.0, 3.5, 4.0]) == pytest.approx(0.482758, abs=1e-5)


def test_implied_probability_ignores_invalid():
    assert implied_probabilities([0.5, 1.1]) == pytest.approx([1.0])
    assert implied_probability(1.0, [1.0, 2.0]) == 0.0
    assert implied_probabilities([]) == []


def test_odds_in_range():
    assert odds_in_range(settings.odds_min)
    assert odds_in_range(settings.odds_max)
    assert not odds_in_range(settings.odds_min - 0.01)
    assert not odds_in_range(settings.odds_max + 0.01)


# --------------------------------------------------------------------------- #
# Исходы 1X2
# --------------------------------------------------------------------------- #
def test_evaluate_outcome_1x2_edge():
    outcomes = [
        outcome("1x2", "home", 2.6),
        outcome("1x2", "draw", 3.4),
        outcome("1x2", "away", 4.2),
    ]
    p_implied = implied_probability(2.6, market_prices_for(outcomes[0], outcomes))
    assert p_implied == pytest.approx(0.4195, abs=5e-4)

    strong = MP(prob_home=0.55, prob_draw=0.25, prob_away=0.20)
    candidate = evaluate_outcome(outcomes[0], strong, outcomes)
    assert candidate is not None
    assert candidate.edge == pytest.approx(0.55 - p_implied, abs=1e-6)
    assert candidate.odds == 2.6 and candidate.odds_source == "winline"

    weak = MP(prob_home=0.44, prob_draw=0.30, prob_away=0.26)
    assert evaluate_outcome(outcomes[0], weak, outcomes) is None


def test_suspicious_line_is_never_a_candidate():
    risky = outcome("1x2", "home", 2.6, suspicious=True)
    probabilities = MP(prob_home=0.9, prob_draw=0.05, prob_away=0.05)
    assert evaluate_outcome(risky, probabilities, [risky]) is None


def test_find_candidates_respects_odds_range():
    home = outcome("1x2", "home", 6.0)  # вне диапазона [1.55, 4.0]
    away = outcome("1x2", "away", 3.0)
    probabilities = MP(prob_home=0.9, prob_draw=0.05, prob_away=0.05)

    # Без фильтра по диапазону исход «home» был бы кандидатом (edge огромный)…
    assert evaluate_outcome(home, probabilities, [home, away]) is not None
    # …но find_candidates обязан его отфильтровать.
    candidates = find_candidates([home, away], probabilities)
    assert all(candidate.selection != "home" for candidate in candidates)


# --------------------------------------------------------------------------- #
# Тоталы (нормальная аппроксимация)
# --------------------------------------------------------------------------- #
def test_prob_over_normal_approximation():
    probabilities = MP(prob_home=0.5, prob_draw=0.25, prob_away=0.25, expected_total=2.7, total_sigma=1.6)
    p_over = probabilities.probability_for("totals", "over", 2.5)
    expected = 1.0 - statistics.NormalDist().cdf((2.5 - 2.7) / 1.6)
    assert p_over == pytest.approx(expected, abs=1e-9)
    assert p_over == pytest.approx(0.549738, abs=1e-5)
    p_under = probabilities.probability_for("totals", "under", 2.5)
    assert p_over + p_under == pytest.approx(1.0)
    assert probabilities.probability_for("totals", "over", None) is None


def test_totals_market_prices_are_per_line():
    over = outcome("totals", "over", 2.1, line=2.5)
    under = outcome("totals", "under", 1.85, line=2.5)
    other_line = outcome("totals", "over", 3.4, line=4.5)  # другая линия — другой рынок
    assert sorted(market_prices_for(over, [over, under, other_line])) == [1.85, 2.1]

    probabilities = MP(prob_home=0.4, prob_draw=0.3, prob_away=0.3, expected_total=3.4, total_sigma=1.5)
    candidate = evaluate_outcome(over, probabilities, [over, under, other_line])
    assert candidate is not None
    assert candidate.prob_implied == pytest.approx(implied_probability(2.1, [1.85, 2.1]), abs=1e-9)


# --------------------------------------------------------------------------- #
# Форы
# --------------------------------------------------------------------------- #
def test_handicap_probabilities():
    probabilities = MP(prob_home=0.6, prob_draw=0.2, prob_away=0.2, expected_margin=1.0, margin_sigma=2.0)
    p_home_hcp = probabilities.probability_for("handicap", "home_handicap", -1.5)
    expected = 1.0 - statistics.NormalDist().cdf((1.5 - 1.0) / 2.0)
    assert p_home_hcp == pytest.approx(expected, abs=1e-9)
    assert p_home_hcp == pytest.approx(0.401294, abs=1e-5)

    p_away_hcp = probabilities.probability_for("handicap", "away_handicap", 1.5)
    assert p_away_hcp == pytest.approx(1.0 - p_home_hcp, abs=1e-12)
    assert probabilities.probability_for("handicap", "home_handicap", None) is None


def test_double_chance_probability():
    probabilities = MP(prob_home=0.5, prob_draw=0.25, prob_away=0.25)
    assert probabilities.probability_for("double_chance", "1x", None) == pytest.approx(0.75)
    assert probabilities.probability_for("double_chance", "x2", None) == pytest.approx(0.50)
    assert probabilities.probability_for("double_chance", "12", None) == pytest.approx(0.75)
    two_way = MP(prob_home=0.6, prob_draw=None, prob_away=0.4)
    assert two_way.probability_for("double_chance", "1x", None) is None


# --------------------------------------------------------------------------- #
# Келли и размер ставки
# --------------------------------------------------------------------------- #
def test_kelly_basic():
    # p=0.55, odds=2.0 → full kelly = 0.1 → 0.25 × 0.1 × 100 = 2.5%
    assert kelly_stake_pct(0.55, 2.0) == pytest.approx(2.5)


def test_kelly_cap():
    # p=0.9, odds=5.0 → full kelly = 0.875 → 21.875% обрезается кэпом 3%
    assert kelly_stake_pct(0.9, 5.0) == pytest.approx(settings.stake_cap_pct)


def test_kelly_rounding_and_negative():
    # p=0.52, odds=2.1 → 0.0836 × 0.25 × 100 = 2.09% → вниз до 2.0% (шаг 0.25%)
    assert kelly_stake_pct(0.52, 2.1) == pytest.approx(2.0)
    assert kelly_stake_pct(0.40, 2.0) == 0.0   # нет перевеса — нет ставки
    assert kelly_stake_pct(0.5, 1.0) == 0.0    # некорректный кэф


# --------------------------------------------------------------------------- #
# Отбор сигналов
# --------------------------------------------------------------------------- #
def _candidate(market: str, selection: str, odds: float, edge: float, line: float | None = None) -> ValueCandidate:
    return ValueCandidate(
        market=market,
        selection=selection,
        line=line,
        odds=odds,
        odds_source="winline",
        prob_final=edge + (1.0 / odds),
        prob_implied=1.0 / odds,
        edge=edge,
    )


def test_select_signals_thresholds_and_limits():
    home = _candidate("1x2", "home", 2.6, 0.13)
    away = _candidate("1x2", "away", 3.0, 0.05)
    selected = select_signals([home, away], confidence_score=0.6, data_quality=DataQuality.OK, min_score=60)
    assert len(selected) == 1 and selected[0][0].selection == "home"

    # weak-данные → сигналов нет вообще (правило из ТЗ)
    assert select_signals([home], confidence_score=0.9, data_quality=DataQuality.WEAK) == []

    # лимит на матч и сортировка по edge
    totals = _candidate("totals", "over", 1.9, 0.10, line=2.5)
    handicap = _candidate("handicap", "home_handicap", 2.4, 0.15, line=-1.5)
    limited = select_signals([home, totals, handicap], confidence_score=0.9, data_quality=DataQuality.OK, max_per_match=2)
    assert len(limited) == 2
    assert [candidate.market for candidate, _ in limited] == ["handicap", "1x2"]


def test_calibration_lowers_score():
    assert composite_score(0.13, 0.6, "ok", calibration=1.0) > composite_score(0.13, 0.6, "ok", calibration=0.8)


def test_signal_payload_contains_everything_needed():
    candidate = _candidate("1x2", "home", 2.6, 0.13)
    probabilities = MP(
        prob_home=0.55,
        prob_draw=0.2,
        prob_away=0.25,
        key_factors=["форма", "травмы"],
        risk_notes=["ротация"],
        reasoning="форма + отсутствие ключевого защитника",
    )
    payload = build_signal_payload(candidate, 71.0, probabilities, data_quality="ok")
    assert payload["edge"] == pytest.approx(0.13)
    assert payload["stake_pct"] > 0
    assert payload["key_factors"] == ["форма", "травмы"]
    assert payload["confidence_score"] == 71.0
    assert payload["reasoning"].startswith("форма")


def test_format_selection_labels():
    assert format_selection("1x2", "home", None) == "П1"
    assert format_selection("1x2", "away", None) == "П2"
    assert format_selection("1x2", "draw", None) == "X"
    assert format_selection("totals", "over", 2.5) == "ТБ 2.5"
    assert format_selection("totals", "under", 2.5) == "ТМ 2.5"
    assert format_selection("handicap", "home_handicap", -1.5) == "Ф1 (-1.5)"
    assert format_selection("handicap", "away_handicap", 1.5) == "Ф2 (+1.5)"
    assert format_selection("double_chance", "1x", None) == "Двойной шанс 1X"
