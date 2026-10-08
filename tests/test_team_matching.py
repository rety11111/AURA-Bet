"""Тесты склейки названий команд (rapidfuzz, порог 85) и кэша xG."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.db.models import Sport, Team, XgCache
from app.sources.team_matching import (
    MATCH_THRESHOLD,
    TeamMatcher,
    load_xg_cache,
    normalize_name,
    similarity,
    upsert_xg_cache,
)


def test_normalize_name_strips_legal_prefixes():
    assert normalize_name("ФК Спартак Мск") == normalize_name("FC Spartak Moscow")
    assert normalize_name("Real Madrid CF") == normalize_name("FC Real Madrid")
    assert normalize_name("Зенит") == "zenit"


def test_similarity_threshold_examples():
    assert similarity("Спартак Мск", "Spartak Moscow") >= MATCH_THRESHOLD
    assert similarity("Манчестер Сити", "Manchester City") >= MATCH_THRESHOLD
    assert similarity("Boston Bruins", "Toronto Maple Leafs") < MATCH_THRESHOLD
    assert similarity("", "anything") == 0.0


@pytest.mark.asyncio
async def test_matcher_merges_transliterated_names(session):
    sport = Sport(code="football", name="Футбол")
    session.add(sport)
    await session.flush()

    matcher = TeamMatcher(session, sport.id)
    first = await matcher.resolve("Спартак Мск", "winline")
    # После фонетической нормализации строки совпадают → exact-хит по нормализованному alias
    second = await matcher.resolve("Spartak Moscow", "apifootball")
    # «Ливерпуль» ↔ «Liverpool» — уже fuzzy-склейка (score ≈ 86 ≥ 85)
    third = await matcher.resolve("Ливерпуль", "winline")
    fourth = await matcher.resolve("Liverpool", "apifootball")

    assert first.team_id is not None and first.team_id == second.team_id
    assert second.matched_by in ("exact", "fuzzy")
    assert third.team_id == fourth.team_id and fourth.matched_by == "fuzzy"

    teams = (await session.execute(select(func.count(Team.id)))).scalar()
    assert teams == 2


@pytest.mark.asyncio
async def test_matcher_does_not_glue_different_teams(session):
    sport = Sport(code="football", name="Футбол")
    session.add(sport)
    await session.flush()

    matcher = TeamMatcher(session, sport.id)
    spartak = await matcher.resolve("Спартак Мск", "winline")
    other = await matcher.resolve("Ювентус", "winline")
    assert other.team_id != spartak.team_id
    assert other.matched_by == "created"
    assert (await session.execute(select(func.count(Team.id)))).scalar() == 2


@pytest.mark.asyncio
async def test_matcher_exact_hit_and_find_team_id(session):
    sport = Sport(code="hockey", name="Хоккей")
    session.add(sport)
    await session.flush()

    matcher = TeamMatcher(session, sport.id)
    resolution = await matcher.resolve("Boston Bruins", "nhl")
    assert resolution.matched_by == "created"
    again = await matcher.resolve("Boston Bruins", "moneypuck")
    assert again.matched_by == "exact"
    assert await matcher.find_team_id("boston bruins") == resolution.team_id
    assert await matcher.find_team_id("Флорида Пантерз") is None


@pytest.mark.asyncio
async def test_xg_cache_roundtrip(session, sport_id):
    team = Team(sport_id=sport_id, canonical_name="Arsenal")
    session.add(team)
    await session.flush()

    payload = {"team": "Arsenal", "matches": 10, "xg_for_per_game": 1.9, "xg_against_per_game": 1.1}
    await upsert_xg_cache(session, team.id, "understat:EPL:2026", payload)
    assert (await session.execute(select(func.count(XgCache.id)))).scalar() == 1

    loaded = await load_xg_cache(session, team.id, "understat:EPL:2026", max_age_sec=3600)
    assert loaded == payload

    # Повторная запись обновляет ту же строку, а не плодит записи
    await upsert_xg_cache(session, team.id, "understat:EPL:2026", {**payload, "matches": 12})
    assert (await session.execute(select(func.count(XgCache.id)))).scalar() == 1
    loaded = await load_xg_cache(session, team.id, "understat:EPL:2026", max_age_sec=3600)
    assert loaded["matches"] == 12

    # Устаревшая запись (max_age_sec=0) не отдаётся
    assert await load_xg_cache(session, team.id, "understat:EPL:2026", max_age_sec=0) is None
    # Другой источник — своя строка
    assert await load_xg_cache(session, team.id, "moneypuck", max_age_sec=3600) is None
