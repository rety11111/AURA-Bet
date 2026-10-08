"""Юнит-тесты Пуассоновской модели (футбол/хоккей) — с вручную посчитанными кейсами."""

from __future__ import annotations

import math

import pytest

from app.sources.base import TeamXgStats
from app.stats_models.poisson import (
    PoissonModel,
    build_attack_defence,
    outcomes_from_matrix,
    poisson_pmf,
    score_matrix,
)


def test_poisson_pmf_hand_computed():
    # e^-2 = 0.135335283...
    assert poisson_pmf(0, 2.0) == pytest.approx(0.1353352832, abs=1e-9)
    # e^-2 × 2^3/3! = 0.1353352832 × 1.3333 = 0.180447
    assert poisson_pmf(3, 2.0) == pytest.approx(0.1804470443, abs=1e-9)
    assert poisson_pmf(1, 1.0) == pytest.approx(math.exp(-1), abs=1e-12)
    assert poisson_pmf(5, 0.0) == 0.0


def test_score_matrix_sums_to_one():
    matrix = score_matrix(1.8, 1.2, max_goals=7)
    total = sum(sum(row) for row in matrix)
    assert total == pytest.approx(1.0, abs=1e-6)
    assert len(matrix) == 8 and len(matrix[0]) == 8


def test_outcomes_from_matrix_means():
    matrix = score_matrix(2.0, 1.0, max_goals=7)
    result = outcomes_from_matrix(matrix)
    assert result["prob_home"] + result["prob_draw"] + result["prob_away"] == pytest.approx(1.0, abs=1e-9)
    # Средние тотала/разницы = λh + λa и λh − λa (с точностью усечения сетки 0..7:
    # для λh=2, λa=1 средние получаются 2.993 и 0.993 — см. комментарий в score_matrix)
    assert result["expected_total"] == pytest.approx(3.0, abs=0.02)
    assert result["expected_margin"] == pytest.approx(1.0, abs=0.02)
    assert result["prob_home"] > result["prob_away"]
    assert 0.0 < result["matrix_sigma_total"] < 3.0


def test_outcomes_symmetric_lambdas():
    matrix = score_matrix(1.6, 1.6, max_goals=7)
    result = outcomes_from_matrix(matrix)
    assert result["prob_home"] == pytest.approx(result["prob_away"], abs=1e-12)
    assert result["expected_margin"] == pytest.approx(0.0, abs=1e-12)
    # P(draw) = Σ pmf(k,λ)² / (Σ pmf(k,λ))² — считаем вручную по определению
    # (делим на квадрат массы, потому что матрица нормируется после усечения сетки)
    mass = sum(poisson_pmf(k, 1.6) for k in range(8))
    manual_draw = sum(poisson_pmf(k, 1.6) ** 2 for k in range(8)) / mass**2
    assert result["prob_draw"] == pytest.approx(manual_draw, abs=1e-12)


def test_build_attack_defence_splits():
    stats = TeamXgStats(
        team="A",
        matches=10,
        xg_for_home=2.0,
        xg_against_home=1.0,
        xg_for_away=1.0,
        xg_against_away=1.5,
    )
    ad = build_attack_defence(stats, league_avg_home=1.6, league_avg_away=1.2)
    assert ad.atk_home == pytest.approx(2.0 / 1.6)
    assert ad.def_home == pytest.approx(1.0 / 1.2)
    assert ad.atk_away == pytest.approx(1.0 / 1.2)
    assert ad.def_away == pytest.approx(1.5 / 1.6)


def test_build_attack_defence_falls_back_to_overall():
    stats = TeamXgStats(team="A", matches=8, xg_for_per_game=1.6, xg_against_per_game=1.2)
    ad = build_attack_defence(stats, league_avg_home=1.5, league_avg_away=1.1)
    assert ad.atk_home == pytest.approx(1.6 / 1.5)
    assert ad.def_away == pytest.approx(1.2 / 1.5)


@pytest.mark.asyncio
async def test_poisson_model_predict_football():
    home = TeamXgStats(team="Home", matches=10, xg_for_home=1.9, xg_against_home=0.9, xg_for_away=1.4, xg_against_away=1.2)
    away = TeamXgStats(team="Away", matches=10, xg_for_home=1.5, xg_against_home=1.1, xg_for_away=1.2, xg_against_away=1.6)
    model = PoissonModel(sport="football")
    prediction = await model.predict(home, away, league_avg_home=1.6, league_avg_away=1.2)
    assert prediction is not None
    assert prediction.model == "poisson"
    assert prediction.data_quality == "ok"
    assert prediction.prob_home + prediction.prob_draw + prediction.prob_away == pytest.approx(1.0, abs=1e-9)
    assert prediction.prob_home > prediction.prob_away          # хозяева сильнее и дома
    lh = prediction.extra["lambda_home"]
    la = prediction.extra["lambda_away"]
    # expected_total берётся из матрицы счётов (обрезана на 7 голах), поэтому чуть ниже λh+λa
    assert prediction.expected_total == pytest.approx(lh + la, abs=0.05)
    assert prediction.expected_total <= lh + la + 1e-9
    # Сигма обязана быть не меньше теоретической sqrt(λh + λa)
    assert prediction.total_sigma >= math.sqrt(lh + la) - 1e-9


@pytest.mark.asyncio
async def test_poisson_model_weak_data_and_missing_stats():
    home = TeamXgStats(team="Home", matches=2, xg_for_per_game=1.5, xg_against_per_game=1.2)
    away = TeamXgStats(team="Away", matches=3, xg_for_per_game=1.2, xg_against_per_game=1.5)
    model = PoissonModel(sport="football")
    prediction = await model.predict(home, away, league_avg_home=1.5, league_avg_away=1.15)
    assert prediction is not None and prediction.data_quality == "weak"
    assert await model.predict(None, away) is None


@pytest.mark.asyncio
async def test_poisson_model_hockey():
    home = TeamXgStats(team="BOS", matches=12, xg_for_per_game=3.2, xg_against_per_game=2.6)
    away = TeamXgStats(team="TOR", matches=12, xg_for_per_game=2.9, xg_against_per_game=2.9)
    model = PoissonModel(sport="hockey")
    prediction = await model.predict(home, away, home_advantage=0.1)
    assert prediction is not None
    assert prediction.model == "poisson_hockey"
    assert 4.0 <= prediction.expected_total <= 8.0
