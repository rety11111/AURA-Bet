"""Юнит-тесты баскетбольной модели (pace / ORtg / DRtg)."""

from __future__ import annotations

import statistics

import pytest

from app.sources.base import BasketballTeamStats
from app.stats_models.basketball import BasketballModel, expected_total_from_ratings


def test_expected_total_formula():
    # pace/100 × (ORtg + DRtg) при равных рейтингах
    assert expected_total_from_ratings(100.0, 114.0, 114.0) == pytest.approx(228.0)
    assert expected_total_from_ratings(96.0, 110.0, 108.0) == pytest.approx(209.28, abs=1e-6)


@pytest.mark.asyncio
async def test_basketball_model_predict_hand_computed():
    home = BasketballTeamStats(team="Celtics", pace=100.0, ortg=116.0, drtg=112.0, games=20)
    away = BasketballTeamStats(team="Lakers", pace=100.0, ortg=112.0, drtg=116.0, games=20)
    model = BasketballModel()
    prediction = await model.predict(home, away, league_key="nba")
    assert prediction is not None and prediction.prob_draw is None

    # eff_home = (116 + 116)/2 = 116 ; eff_away = (112 + 112)/2 = 112
    assert prediction.expected_total == pytest.approx(100.0 / 100.0 * (116.0 + 112.0))
    # margin = 1.0 × (116 − 112) + 2.5 = 6.5
    assert prediction.expected_margin == pytest.approx(6.5)
    # prob_home = Φ(6.5 / 12)
    expected_prob = statistics.NormalDist().cdf(6.5 / 12.0)
    assert prediction.prob_home == pytest.approx(expected_prob, abs=1e-9)
    assert prediction.prob_home + prediction.prob_away == pytest.approx(1.0)
    assert prediction.data_quality == "ok"
    assert prediction.total_sigma == pytest.approx(14.0)


@pytest.mark.asyncio
async def test_basketball_model_weak_without_ratings():
    home = BasketballTeamStats(team="A", pace=None, ortg=None, drtg=None, games=12)
    away = BasketballTeamStats(team="B", pace=None, ortg=None, drtg=None, games=12)
    model = BasketballModel()
    prediction = await model.predict(home, away, league_key="euroleague")
    assert prediction is not None
    assert prediction.data_quality == "weak"
    assert prediction.expected_total == pytest.approx(75.0 / 100.0 * (108.0 + 108.0), abs=1e-6)


@pytest.mark.asyncio
async def test_basketball_model_returns_none_without_stats():
    model = BasketballModel()
    assert await model.predict(None, None) is None
