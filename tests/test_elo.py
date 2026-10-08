"""Юнит-тесты Elo-модели киберспорта (включая обновление в БД)."""

from __future__ import annotations

import pytest

from app.db.models import Sport, Team
from app.stats_models.elo import (
    EloModel,
    apply_match_result,
    elo_from_margin,
    expected_score,
    get_elo,
    update_rating,
)


def test_expected_score_hand_computed():
    assert expected_score(1500, 1500) == pytest.approx(0.5)
    # 1 / (1 + 10^((1400-1600)/400)) = 1 / (1 + 10^-0.5) = 0.759747
    assert expected_score(1600, 1400) == pytest.approx(0.759747, abs=1e-6)
    assert expected_score(1400, 1600) == pytest.approx(1 - 0.759747, abs=1e-6)
    # Домашнее преимущество сдвигает вероятность
    assert expected_score(1500, 1500, home_advantage=20) == pytest.approx(0.528, abs=1e-3)


def test_update_rating_hand_computed():
    assert update_rating(1500, 1.0, 0.5, k=32) == pytest.approx(1516.0)
    assert update_rating(1500, 0.0, 0.5, k=32) == pytest.approx(1484.0)
    assert update_rating(1600, 0.5, 0.5, k=32) == pytest.approx(1600.0)


def test_elo_from_margin():
    assert elo_from_margin(8, scale=8) == pytest.approx(1.0)
    assert elo_from_margin(-8, scale=8) == pytest.approx(0.0)
    assert elo_from_margin(0, scale=8) == pytest.approx(0.5)
    assert elo_from_margin(4, scale=8) == pytest.approx(0.75)


@pytest.mark.asyncio
async def test_elo_model_prediction():
    model = EloModel()
    prediction = await model.predict(1600, 1400, matches_home=20, matches_away=20)
    assert prediction.prob_home == pytest.approx(0.759747, abs=1e-6)
    assert prediction.prob_draw is None
    assert prediction.prob_away == pytest.approx(0.240253, abs=1e-6)
    assert prediction.data_quality == "ok"
    assert prediction.expected_margin > 0


@pytest.mark.asyncio
async def test_elo_model_map_specific_and_weak():
    model = EloModel()
    prediction = await model.predict(1500, 1500, map_name="Mirage", matches_home=1, matches_away=0)
    assert prediction.model == "elo_mirage"
    assert prediction.data_quality == "weak"
    assert prediction.prob_home == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_apply_match_result_updates_map_elo(session):
    sport = Sport(code="cs2", name="CS2")
    session.add(sport)
    home = Team(sport=sport, canonical_name="Team Spirit")
    away = Team(sport=sport, canonical_name="Natus Vincere")
    session.add_all([home, away])
    await session.flush()

    new_home, new_away = await apply_match_result(
        session, sport.id, home.id, away.id, home_won=True, map_name="Nuke", margin=13 - 9
    )
    assert new_home > 1500 > new_away
    stored_home, matches_home = await get_elo(session, sport.id, home.id, "Nuke")
    stored_away, matches_away = await get_elo(session, sport.id, away.id, "Nuke")
    assert stored_home == pytest.approx(new_home)
    assert stored_away == pytest.approx(new_away)
    assert matches_home == 1 and matches_away == 1

    # Общий Elo (map_name=None) не изменился — рейтинги по картам независимы
    overall_home, overall_matches = await get_elo(session, sport.id, home.id, None)
    assert overall_home == pytest.approx(1500.0)
    assert overall_matches == 0
