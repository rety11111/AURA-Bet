"""Тесты расчёта исхода сигналов (results_tracker.settle_signal) и Brier/весов."""

from __future__ import annotations

import pytest

from app.db.models import Signal, SignalStatus
from app.learning.calibration import brier_score, weights_from_brier


def signal(market: str, selection: str, line: float | None = None, odds: float = 2.0) -> Signal:
    return Signal(
        match_id=1,
        market=market,
        selection=selection,
        line=line,
        odds=odds,
        prob_final=0.55,
        prob_implied=0.5,
        edge=0.05,
        confidence_score=70.0,
        stake_pct=1.0,
        status=SignalStatus.SENT,
    )


# --------------------------------------------------------------------------- #
# 1X2 и двойной шанс
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("selection", "home", "away", "expected"),
    [
        ("home", 2, 0, SignalStatus.WON),
        ("home", 1, 1, SignalStatus.LOST),
        ("away", 0, 1, SignalStatus.WON),
        ("away", 3, 0, SignalStatus.LOST),
        ("draw", 1, 1, SignalStatus.WON),
        ("draw", 2, 1, SignalStatus.LOST),
    ],
)
def test_settle_1x2(selection, home, away, expected):
    assert settle(signal("1x2", selection), home, away) == expected


@pytest.mark.parametrize(
    ("selection", "home", "away", "expected"),
    [
        ("1x", 1, 1, SignalStatus.WON),
        ("1x", 0, 1, SignalStatus.LOST),
        ("x2", 0, 0, SignalStatus.WON),
        ("x2", 2, 0, SignalStatus.LOST),
        ("12", 2, 0, SignalStatus.WON),
        ("12", 1, 1, SignalStatus.LOST),
    ],
)
def test_settle_double_chance(selection, home, away, expected):
    assert settle(signal("double_chance", selection), home, away) == expected


# --------------------------------------------------------------------------- #
# Тоталы
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("selection", "line", "home", "away", "expected"),
    [
        ("over", 2.5, 2, 1, SignalStatus.WON),
        ("over", 2.5, 1, 1, SignalStatus.LOST),
        ("under", 2.5, 1, 1, SignalStatus.WON),
        ("under", 2.5, 2, 1, SignalStatus.LOST),
        ("over", 3.0, 3, 0, SignalStatus.VOID),   # ровно в линию — возврат
        ("under", 3.0, 2, 1, SignalStatus.VOID),
        ("over", 3.0, 3, 1, SignalStatus.WON),
        ("under", 4.5, 0, 0, SignalStatus.WON),
    ],
)
def test_settle_totals(selection, line, home, away, expected):
    assert settle(signal("totals", selection, line), home, away) == expected


# --------------------------------------------------------------------------- #
# Форы
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("selection", "line", "home", "away", "expected"),
    [
        ("home_handicap", -1.5, 2, 0, SignalStatus.WON),   # margin 2 > 1.5
        ("home_handicap", -1.5, 1, 0, SignalStatus.LOST),  # margin 1 < 1.5
        ("home_handicap", -1.0, 2, 1, SignalStatus.VOID),  # margin ровно 1 → возврат
        ("home_handicap", -1.0, 3, 1, SignalStatus.WON),
        ("home_handicap", 1.5, 0, 1, SignalStatus.WON),    # плюсовая фора хозяевам
        ("away_handicap", 1.5, 2, 1, SignalStatus.WON),    # margin 1 < 1.5
        ("away_handicap", 1.5, 3, 0, SignalStatus.LOST),   # margin 3 > 1.5
        ("away_handicap", -1.5, 0, 2, SignalStatus.WON),   # гости отдают 1.5
        ("away_handicap", 1.0, 2, 1, SignalStatus.VOID),   # margin ровно 1 → возврат
    ],
)
def test_settle_handicap(selection, line, home, away, expected):
    assert settle(signal("handicap", selection, line), home, away) == expected


def settle(sig: Signal, home: int, away: int) -> str:
    from app.tracking.results_tracker import settle_signal

    return settle_signal(sig, home, away)


def test_settle_unknown_market_is_void():
    assert settle(signal("some_new_market", "home"), 1, 0) == SignalStatus.VOID
    assert settle(signal("totals", "over", None), 1, 0) == SignalStatus.VOID


# --------------------------------------------------------------------------- #
# ROI-формула (проверяем на числах из ТЗ)
# --------------------------------------------------------------------------- #
def test_roi_math_hand_computed():
    # 2 выигранных по 1% банка с кэфами 2.0 и 3.0 + 1 проигранная 1%:
    # профит = (1×1.0 + 1×2.0) − 1 = +2.0; ставок 3 → ROI = +0.6667
    staked = 1.0 + 1.0 + 1.0
    profit = 1.0 * (2.0 - 1.0) + 1.0 * (3.0 - 1.0) - 1.0
    assert round(profit / staked, 4) == 0.6667


# --------------------------------------------------------------------------- #
# Brier score и веса ансамбля
# --------------------------------------------------------------------------- #
def test_brier_score_hand_computed():
    # (0.8−1)² + (0.3−0)^2 = 0.04 + 0.09 → /2 = 0.065
    assert brier_score([0.8, 0.3], [1, 0]) == pytest.approx(0.065)
    assert brier_score([], []) is None
    assert brier_score([0.5], [1, 0]) is None  # длины не совпадают


def test_weights_from_brier_prefers_better_model():
    # Сырой вес LLM = brier_stat/(brier_llm+brier_stat) = 0.30/0.40 = 0.75,
    # но сжимается к [0.3, 0.7] → 0.70 / 0.30
    w_llm, w_stat = weights_from_brier(brier_llm=0.10, brier_stat=0.30)
    assert w_llm == pytest.approx(0.7) and w_stat == pytest.approx(0.3)

    # Обратный случай: стат-модель точнее → её вес 0.7, LLM упирается в нижний кламп
    w_llm, w_stat = weights_from_brier(brier_llm=0.30, brier_stat=0.10)
    assert w_llm == pytest.approx(0.3) and w_stat == pytest.approx(0.7)
    assert w_llm + w_stat == pytest.approx(1.0)

    # Внутри диапазона вес не искажается: 0.22/0.20 → 0.476 / 0.524
    w_llm, w_stat = weights_from_brier(brier_llm=0.22, brier_stat=0.20)
    assert w_llm == pytest.approx(0.476, abs=1e-3) and w_stat == pytest.approx(0.524, abs=1e-3)


def test_weights_from_brier_degenerate():
    from app.config import settings

    assert weights_from_brier(0.0, 0.0) == (settings.w_llm_init, settings.w_stat_init)
