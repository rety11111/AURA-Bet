"""Регрессии NHL-резолвера, хоккейного фолбэка и контекста прематча."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from app.config import settings
from app.db.models import Match, MatchStatus
from app.pipeline import analyzer
from app.sources.base import TeamXgStats
from app.sources.nhl import NhlProvider


async def test_nhl_resolves_exact_fuzzy_and_missing_team(monkeypatch: pytest.MonkeyPatch) -> None:
    nhl = NhlProvider()

    async def teams() -> dict[str, str]:
        return {
            "toronto maple leafs": "TOR", "boston bruins": "BOS",
            "new york rangers": "NYR", "new york islanders": "NYI",
        }

    monkeypatch.setattr(nhl, "_teams_index", teams)
    try:
        assert await nhl.resolve_team("TOR") == "TOR"
        assert await nhl.resolve_team("Toronto Maple Leafs") == "TOR"
        assert await nhl.resolve_team("Maple Leafs") == "TOR"
        assert await nhl.resolve_team("New York") is None
        assert await nhl.resolve_team("unrelated club") is None
        monkeypatch.setattr(settings, "team_match_threshold", 95.0)
        assert await nhl.resolve_team("Maple Leafs") is None
    finally:
        await nhl.aclose()


async def test_nhl_goal_stats_keep_real_sample_and_source(monkeypatch: pytest.MonkeyPatch) -> None:
    nhl = NhlProvider()

    async def teams() -> dict[str, str]:
        return {"toronto maple leafs": "TOR"}

    async def schedule(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        return [{
            "gameState": "OFF", "gameDate": "2026-10-01",
            "homeTeam": {"abbrev": "TOR", "score": 4},
            "awayTeam": {"abbrev": "BOS", "score": 2},
        }]

    monkeypatch.setattr(nhl, "_teams_index", teams)
    monkeypatch.setattr(nhl, "_club_schedule", schedule)
    try:
        stats = await nhl.get_team_goal_stats("Toronto Maple Leafs")
        assert stats is not None
        assert (stats.matches, stats.xg_for_per_game, stats.xg_against_per_game) == (1, 4, 2)
        assert "фолбэк" in stats.source
        assert await nhl.get_team_goal_stats("unrelated club") is None
    finally:
        await nhl.aclose()


async def test_nhl_schedule_density_handles_first_finished_game(monkeypatch: pytest.MonkeyPatch) -> None:
    nhl = NhlProvider()

    async def teams() -> dict[str, str]:
        return {"toronto maple leafs": "TOR"}

    async def schedule(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        return [{
            "gameState": "OFF",
            "gameDate": (datetime.now(UTC) - timedelta(days=2)).date().isoformat(),
        }]

    monkeypatch.setattr(nhl, "_teams_index", teams)
    monkeypatch.setattr(nhl, "_club_schedule", schedule)
    try:
        density = await nhl.get_schedule_density("Toronto Maple Leafs")
        assert density.matches_last_7_days == 1
        assert density.rest_days is not None
    finally:
        await nhl.aclose()


@pytest.mark.parametrize("has_xg", [True, False])
async def test_hockey_prediction_uses_xg_or_nhl_goal_fallback(
    monkeypatch: pytest.MonkeyPatch, has_xg: bool
) -> None:
    calls: list[str] = []
    saved: list[Any] = []

    class NHL:
        async def resolve_team(self, name: str) -> str | None:
            return {"Toronto": "TOR", "Boston": "BOS"}.get(name)

        async def get_team_goal_stats(self, name: str) -> TeamXgStats | None:
            calls.append(name)
            return TeamXgStats(team=name, matches=8, xg_for_per_game=3.1, xg_against_per_game=2.8, source="nhl:goals")

    class MoneyPuck:
        async def get_team_xg(self, abbrev: str) -> TeamXgStats | None:
            return (
                TeamXgStats(team=abbrev, matches=30, xg_for_per_game=3.2, xg_against_per_game=2.7, source="moneypuck")
                if has_xg and abbrev == "TOR" else None
            )

    async def save(_session: Any, _match_id: int, prediction: Any) -> None:
        saved.append(prediction)

    monkeypatch.setattr(analyzer, "_store_stat_prediction", save)
    prediction = await analyzer._hockey_prediction(
        None, SimpleNamespace(id=1), SimpleNamespace(nhl=NHL(), moneypuck=MoneyPuck()), "Toronto", "Boston"
    )
    assert prediction is not None
    assert prediction.sample_size == 8
    assert calls == (["Boston"] if has_xg else ["Toronto", "Boston"])
    assert saved == [prediction]


async def test_hockey_prediction_skips_when_fallback_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    class NHL:
        async def resolve_team(self, name: str) -> None:
            return None

        async def get_team_goal_stats(self, name: str) -> None:
            return None

    class MoneyPuck:
        async def get_team_xg(self, abbrev: str) -> None:
            pytest.fail("не сопоставленная NHL-команда не должна запрашивать MoneyPuck")

    assert await analyzer._hockey_prediction(
        None, SimpleNamespace(id=1), SimpleNamespace(nhl=NHL(), moneypuck=MoneyPuck()), "?", "?"
    ) is None


@pytest.mark.parametrize("is_live,stage", [(False, None), (True, "карта 1")])
async def test_build_context_uses_live_context_not_missing_orm_column(
    session: Any, sport_id: int, monkeypatch: pytest.MonkeyPatch, is_live: bool, stage: str | None
) -> None:
    match = Match(
        sport_id=sport_id, ext_id="context-match", league="NHL", status=MatchStatus.SCHEDULED,
        starts_at=datetime.now(UTC),
    )
    session.add(match)
    await session.flush()

    async def no_insights(*_args: Any) -> None:
        return None

    async def no_stats(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {}

    monkeypatch.setattr(analyzer, "_league_insights", no_insights)
    monkeypatch.setattr(analyzer, "_context_stats", no_stats)
    context = await analyzer.build_context(
        session, match, "hockey", [], None, SimpleNamespace(), home_name="Toronto", away_name="Boston",
        is_live=is_live, live_context={"stage": "карта 1"},
    )
    assert context["match"]["live_stage"] == stage
    assert context["match"]["match_id"] == match.id
