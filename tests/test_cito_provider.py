"""Cito API: расписание CS2/Dota 2, tier, карты, ростеры + бюджет free-плана.

Ключевые проверки:
  * без ключа источник выключен (Cito отдаёт данные только с `x-api-key`);
  * строки матчей Cito нормализуются в наш `Match` (team1Name → home_team и т.д.);
  * карта `id`/`event.tier`/`prestigeScore` → tier, который понимает скринер;
  * статистика по карте берётся из `mapWinRates` (замена Liquipedia get_map_stats);
  * 401/403 выключает источник, 429/сеть — просто сбой;
  * месячный бюджет останавливает запросы (free-план — 500/мес).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from app.config import settings
from app.pipeline.collector import esports_event_to_match
from app.sources.base import EsportsLiveMatch, MonthlyBudget, SourceError
from app.sources.cito import CitoEsportsProvider, team_slug, tier_label

CS2_MATCH_ROW = {
    "entityType": "cs2_match",
    "id": "cs2-match-2398521",
    "eventId": "cs2-event-9356",
    "eventName": "ESL Challenger League Season 52 North America Cup 2",
    "stageName": "Upper bracket",
    "startsAt": "2030-01-01T01:00:00.000Z",
    "bestOf": 3,
    "status": "upcoming",
    "team1Name": "Club 333",
    "team2Name": "DETONATE",
    "team1Score": None,
    "team2Score": None,
    "event": {
        "id": "cs2-event-9356",
        "name": "ESL Challenger League Season 52 North America Cup 2",
        "tier": "A",
        "prestigeScore": 80,
    },
}


def make_provider(**kwargs: Any) -> CitoEsportsProvider:
    return CitoEsportsProvider(api_key="test-key", **kwargs)


def test_team_slug_and_tier_label() -> None:
    assert team_slug("Natus Vincere") == "natus-vincere"
    assert team_slug("  G2  ") == "g2"
    assert tier_label("s") == "S-Tier"
    assert tier_label("A") == "A-Tier"
    assert tier_label(None, 88) == "S-Tier"
    assert tier_label(None, 55) == "B-Tier"
    assert tier_label(None, None) is None


def test_provider_disabled_without_key() -> None:
    provider = CitoEsportsProvider(api_key="")
    assert provider.available is False
    assert "CITO_API_KEY" in (provider.disabled_reason or "")


def test_provider_enabled_with_key() -> None:
    provider = make_provider()
    assert provider.available is True
    assert provider.headers["x-api-key"] == "test-key"


@pytest.mark.asyncio
async def test_get_upcoming_matches_normalizes_rows() -> None:
    provider = make_provider()
    calls: list[tuple[str, dict[str, Any]]] = []

    async def fake_request(path: str, params: dict[str, Any] | None = None) -> Any:
        calls.append((path, params or {}))
        return [CS2_MATCH_ROW]

    provider._request = fake_request  # type: ignore[method-assign]
    matches = await provider.get_upcoming_matches("cs2", days=3650)
    assert len(matches) == 1
    match = matches[0]
    assert (match.home_team, match.away_team) == ("Club 333", "DETONATE")
    assert match.league_tier == "A-Tier"
    assert match.ext_id == "cs2-match-2398521"
    assert match.source == "cito"
    assert match.extra["bestof"] == 3
    assert calls and calls[0][0] == "/cs2/matches/upcoming"


@pytest.mark.asyncio
async def test_upcoming_is_cached_between_calls() -> None:
    provider = make_provider()
    calls = 0

    async def fake_request(path: str, params: dict[str, Any] | None = None) -> Any:
        nonlocal calls
        calls += 1
        return [CS2_MATCH_ROW]

    provider._request = fake_request  # type: ignore[method-assign]
    await provider.get_upcoming_matches("cs2", days=2)
    await provider.get_upcoming_matches("cs2", days=2)
    assert calls == 1  # второй вызов — из кэша (бережём 500 запросов/мес)


@pytest.mark.asyncio
async def test_unsupported_sport_makes_no_request() -> None:
    provider = make_provider()

    async def refuse(*_a: Any, **_k: Any) -> Any:  # pragma: no cover
        raise AssertionError("запрос не должен уходить для неподдерживаемого спорта")

    provider._request = refuse  # type: ignore[method-assign]
    assert await provider.get_upcoming_matches("lol") == []
    assert await provider.get_live_matches("valorant") == []


@pytest.mark.asyncio
async def test_map_stats_from_map_win_rates() -> None:
    provider = make_provider()

    async def fake_request(path: str, params: dict[str, Any] | None = None) -> Any:
        assert path == "/cs2/teams/vitality/stats"
        return {
            "teamId": "cs2-team-9565",
            "mapWinRates": [
                {"mapName": "Mirage", "played": 140, "wins": 76, "winRate": 0.5428},
                {"mapName": "Inferno", "played": 134, "wins": 81, "winRate": 0.6045},
            ],
        }

    provider._request = fake_request  # type: ignore[method-assign]
    stats = await provider.get_map_stats("Vitality", "Mirage")
    assert stats is not None
    assert stats.matches == 140
    assert stats.win_rate == 0.5428
    assert stats.round_win_rate is None
    assert stats.source == "cito:mapWinRates"
    assert await provider.get_map_stats("Vitality", "Nuke") is None


@pytest.mark.asyncio
async def test_team_roster_cs2_only() -> None:
    provider = make_provider()

    async def fake_request(path: str, params: dict[str, Any] | None = None) -> Any:
        assert path == "/cs2/teams/vitality/roster"
        return [
            {"playerNickname": "ZywOo", "role": "AWPer"},
            {"playerNickname": "XTQZZZ", "role": "coach"},
        ]

    provider._request = fake_request  # type: ignore[method-assign]
    assert await provider.get_team_roster("cs2", "Vitality") == ["ZywOo", "XTQZZZ"]
    assert await provider.get_team_roster("dota2", "Team Spirit") is None


@pytest.mark.asyncio
async def test_tournament_tier_from_event_catalog() -> None:
    provider = make_provider()
    paths: list[str] = []

    async def fake_request(path: str, params: dict[str, Any] | None = None) -> Any:
        paths.append(path)
        if path == "/cs2/events/upcoming":
            return [{"name": "BLAST Premier World Final 2030", "tier": "S", "prestigeScore": 95}]
        return []

    provider._request = fake_request  # type: ignore[method-assign]
    assert await provider.get_tournament_tier("BLAST Premier World Final 2030", sport="cs2") == "S-Tier"
    # Нечёткое совпадение названий источников (Liquipedia пишет по-своему).
    assert await provider.get_tournament_tier("BLAST World Final 2030", sport="cs2") == "S-Tier"
    assert await provider.get_tournament_tier("Какой-то левый кубок", sport="cs2") is None
    assert await provider.get_tournament_tier("The International", sport="dota2") is None


@pytest.mark.asyncio
async def test_auth_error_disables_provider() -> None:
    provider = make_provider()
    calls = 0

    async def fake_json(url: str, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        raise SourceError("cito: HTTP 401 на https://api.citoapi.com/api/v1/cs2/matches/upcoming")

    provider.get_json = fake_json  # type: ignore[method-assign]
    assert await provider.get_upcoming_matches("cs2", days=2) == []
    assert provider.available is False
    assert "ключ" in (provider.disabled_reason or "")
    assert await provider.get_upcoming_matches("cs2", days=2) == []
    assert calls == 1  # второй раз в сеть не ходим


def test_monthly_budget_stops_requests() -> None:
    budget = MonthlyBudget(limit=2, window_sec=10.0)
    assert budget.take(now=0.0) and budget.take(now=1.0)
    assert budget.take(now=2.0) is False
    assert budget.remaining == 0
    assert budget.take(now=11.0) is True  # первый запрос вышел из окна


@pytest.mark.asyncio
async def test_budget_exhaustion_silences_source() -> None:
    """Бюджет проверяется в `_request`, поэтому подменяем только сетевой слой."""
    provider = make_provider(monthly_limit=1)
    calls = 0

    async def fake_json(url: str, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        return {"success": True, "data": [CS2_MATCH_ROW]}

    provider.get_json = fake_json  # type: ignore[method-assign]
    await provider.get_upcoming_matches("cs2", days=2)
    await provider.get_upcoming_matches("dota2", days=2)  # бюджет уже исчерпан
    assert calls == 1
    assert provider.budget_remaining == 0


@pytest.mark.asyncio
async def test_live_matches_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "cito_live_enabled", False)
    provider = make_provider()

    async def refuse(*_a: Any, **_k: Any) -> Any:  # pragma: no cover
        raise AssertionError("лайв-опрос выключен по умолчанию")

    provider._request = refuse  # type: ignore[method-assign]
    assert await provider.get_live_matches("cs2") == []


@pytest.mark.asyncio
async def test_live_matches_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "cito_live_enabled", True)
    provider = make_provider()
    row = {
        **CS2_MATCH_ROW,
        "status": "live",
        "team1Score": 1,
        "team2Score": 0,
        "currentMap": "Mirage",
    }

    async def fake_request(path: str, params: dict[str, Any] | None = None) -> Any:
        assert path == "/cs2/live"
        return [row]

    provider._request = fake_request  # type: ignore[method-assign]
    live = await provider.get_live_matches("cs2")
    assert len(live) == 1
    assert live[0].series_score == (1, 0)
    assert live[0].map_name == "Mirage"


# --------------------------------------------------------------------------- #
# Конвертация события киберспорта в Match (баг: upsert_match ждал home_team)
# --------------------------------------------------------------------------- #
def test_esports_event_to_match_converts_live_match() -> None:
    event = EsportsLiveMatch(
        ext_id="cs2-match-1",
        sport="cs2",
        league="BLAST Premier",
        league_tier="S-Tier",
        team_a="Vitality",
        team_b="Spirit",
        source="cito",
        match_time=datetime.now(timezone.utc) + timedelta(hours=3),
    )
    match = esports_event_to_match(event, "cs2")
    assert match is not None
    assert match.home_team == "Vitality" and match.away_team == "Spirit"
    assert match.starts_at == event.match_time
    assert match.league_tier == "S-Tier"
    assert match.source == "cito"


def test_esports_event_to_match_skips_incomplete() -> None:
    event = EsportsLiveMatch(
        ext_id="x", sport="cs2", league="L", team_a="A", team_b="B", source="cito", match_time=None
    )
    assert esports_event_to_match(event, "cs2") is None


# --------------------------------------------------------------------------- #
# Сквозной путь коллектора: Cito → upsert_match (ранее падало с AttributeError)
# --------------------------------------------------------------------------- #
class _FakeCito:
    """Минимальный двойник Cito: один матч CS2 и один Dota 2."""

    provider_name = "cito"
    available = True

    def __init__(self) -> None:
        self.tier_calls: list[tuple[str, str]] = []

    async def get_upcoming_matches(self, sport: str, days: int = 2) -> list[EsportsLiveMatch]:
        start = datetime.now(timezone.utc) + timedelta(hours=4)
        return [
            EsportsLiveMatch(
                ext_id=f"{sport}-1",
                sport=sport,
                league="ESL One Test",
                league_tier=None,
                team_a="Team Alpha",
                team_b="Team Beta",
                source="cito",
                match_time=start,
            )
        ]

    async def get_tournament_tier(self, tournament: str, sport: str = "cs2") -> str | None:
        self.tier_calls.append((tournament, sport))
        return "S-Tier"


@pytest.mark.asyncio
async def test_collect_day_schedule_upserts_cito_matches(session: Any) -> None:
    """События Cito доходят до БД: конвертация EsportsLiveMatch → Match → upsert_match."""
    from sqlalchemy import select

    from app.db.models import Match as MatchRow, Sport
    from app.pipeline.collector import Providers, collect_day_schedule

    session.add(Sport(code="cs2", name="CS2"))
    session.add(Sport(code="dota2", name="Dota 2"))
    await session.flush()

    fake = _FakeCito()
    providers = Providers(cito=fake)  # type: ignore[arg-type]
    stats = await collect_day_schedule(session, providers=providers)  # type: ignore[arg-type]

    assert stats["cito"] == 2
    rows = (await session.execute(select(MatchRow).where(MatchRow.ext_id.like("cito:%")))).scalars().all()
    assert len(rows) == 2
    assert {row.league_tier for row in rows} == {"S-Tier"}  # tier подтянулся до прохода скринера
    assert fake.tier_calls


@pytest.mark.asyncio
async def test_same_team_name_in_two_sports_does_not_crash(session: Any) -> None:
    """Одна организация в CS2 и Dota 2 (Team Spirit и т.п.) не должна ронять матчинг.

    В `team_aliases` уникальность (alias, source) без вида спорта — повторный alias
    раньше давал IntegrityError и скрытно терял склейку команд.
    """
    from sqlalchemy import select

    from app.db.models import Match as MatchRow, Sport
    from app.pipeline.collector import Providers, collect_day_schedule

    session.add(Sport(code="cs2", name="CS2"))
    session.add(Sport(code="dota2", name="Dota 2"))
    await session.flush()

    providers = Providers(cito=_FakeCito())  # type: ignore[arg-type]
    stats = await collect_day_schedule(session, providers=providers)  # type: ignore[arg-type]

    assert stats["cito"] == 2  # оба матча склеились, несмотря на одинаковые имена команд
    rows = (await session.execute(select(MatchRow))).scalars().all()
    assert len(rows) == 2
    assert len({row.home_team_id for row in rows}) == 2  # команды разных спортов — разные


@pytest.mark.asyncio
async def test_events_outside_days_window_are_dropped() -> None:
    """В окно `days` попадают только ближние матчи: иначе «расписание на 2 дня»
    незаметно превращается в расписание на неделю, а free-план тратится на мусор."""
    provider = make_provider()
    now = datetime.now(timezone.utc)
    rows = [
        dict(CS2_MATCH_ROW, id="cs2-match-near", startsAt=(now + timedelta(days=1)).isoformat()),
        dict(CS2_MATCH_ROW, id="cs2-match-far", startsAt=(now + timedelta(days=9)).isoformat()),
        dict(CS2_MATCH_ROW, id="cs2-match-past", startsAt=(now - timedelta(days=2)).isoformat()),
    ]

    async def fake_request(path: str, params: dict[str, Any] | None = None) -> Any:
        return rows

    provider._request = fake_request  # type: ignore[method-assign]
    matches = await provider.get_upcoming_matches("cs2", days=2)
    assert [m.ext_id for m in matches] == ["cs2-match-near"]
