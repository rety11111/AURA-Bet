"""1xBet API (RapidAPI): парсинг рынков и защита free-плана.

Проверяем то, что легко сломать:
  * рынки `markets_v2` (`{name, base, odds{...}}`) разбираются в наши Odd;
  * исходы понимают и латиницу, и кириллицу («П1», «Тотал Больше 2.5»);
  * `mode=line|live` передаётся в запрос рынков (иначе провайдер отдаёт 404);
  * без RAPIDAPI_KEY/ONEXBET_ENABLED источник выключен;
  * месячный бюджет (free-план 500) закрывает источник, а не выжигает лимит 429-ми.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

import pytest

from app.config import settings
from app.sources.base import SourceError
from app.sources.onexbet import OnexbetOddsProvider, parse_markets

MARKETS_V2 = [
    {"base": None, "market_id": 8, "name": "Double Chance",
     "odds": {"HOME Or X": 1.289, "HOME Or AWAY": 1.296, "AWAY Or X": 1.922}},
    {"base": -3.5, "market_id": 2, "name": "Handicap", "odds": {"Handicap HOME ": 14.5}},
    {"base": 2.5, "market_id": 3, "name": "Total", "odds": {"Over": 1.9, "Under": 1.95}},
    {"base": None, "market_id": 1, "name": "1x2", "odds": {"W1": 1.7, "X": 3.4, "W2": 4.2}},
    {"base": None, "market_id": 15, "name": "Total",
     "odds": {"Total Over 2.5": 2.05, "Total Under 2.5": 1.75}},
    {"base": None, "market_id": 99, "name": "Unknown market", "odds": {"whatever": 2.0}},
]


def make_provider(**kwargs: Any) -> OnexbetOddsProvider:
    return OnexbetOddsProvider(api_key="rapid-key", host="1xbet-api.p.rapidapi.com", **kwargs)


def test_parse_markets_v2() -> None:
    odds = parse_markets(MARKETS_V2)
    assert {(o.market, o.selection, o.line) for o in odds} == {
        ("double_chance", "1x", None),
        ("double_chance", "12", None),
        ("double_chance", "x2", None),
        ("handicap", "home_handicap", -3.5),
        ("totals", "over", 2.5),
        ("totals", "under", 2.5),
        ("1x2", "home", None),
        ("1x2", "draw", None),
        ("1x2", "away", None),
        ("totals", "over", 2.5),
        ("totals", "under", 2.5),
    }
    assert all(o.price > 1.0 and o.source == "1xbet" for o in odds)
    # «Unknown market» игнорируем, а не тащим мусор в агрегатор.
    assert all("whatever" not in o.selection for o in odds)


def test_parse_markets_cyrillic() -> None:
    rows = [
        {"base": None, "name": "1x2", "odds": {"П1": 1.5, "Х": 4.0, "П2": 6.0}},
        {"base": None, "name": "Тотал", "odds": {"Тотал Больше 2.5": 1.85, "Тотал Меньше 2.5": 1.95}},
        {"base": None, "name": "Двойной шанс", "odds": {"1Х": 1.2, "12": 1.3, "Х2": 2.4}},
    ]
    odds = parse_markets(rows)
    assert {(o.market, o.selection) for o in odds} == {
        ("1x2", "home"), ("1x2", "draw"), ("1x2", "away"),
        ("totals", "over"), ("totals", "under"),
        ("double_chance", "1x"), ("double_chance", "12"), ("double_chance", "x2"),
    }
    total = next(o for o in odds if o.market == "totals" and o.selection == "over")
    assert total.line == 2.5  # линия вытащена из текста исхода


def test_parse_markets_accepts_list_outcomes() -> None:
    rows = [
        {"name": "Match Winner", "odds": [{"name": "Home", "price": 1.8}, {"name": "Away", "price": 2.05}]}
    ]
    odds = parse_markets(rows)
    assert {(o.selection, o.price) for o in odds} == {("home", 1.8), ("away", 2.05)}


def test_provider_disabled_without_key_or_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "onexbet_enabled", True)
    provider = OnexbetOddsProvider(api_key="")
    assert provider.available is False
    assert "RAPIDAPI_KEY" in (provider.disabled_reason or "")

    monkeypatch.setattr(settings, "onexbet_enabled", False)
    provider2 = OnexbetOddsProvider(api_key="key", host="1xbet-api.p.rapidapi.com")
    assert provider2.available is False


def test_provider_enabled_and_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "onexbet_enabled", True)
    provider = make_provider()
    assert provider.available is True
    assert provider.base_url == "https://1xbet-api.p.rapidapi.com"
    assert provider.headers["X-RapidAPI-Key"] == "rapid-key"
    assert provider.headers["X-RapidAPI-Host"] == "1xbet-api.p.rapidapi.com"


def test_default_supported_sports_is_esports() -> None:
    provider = make_provider()
    assert provider.supported_sports == ("cs2", "dota2")


@pytest.mark.asyncio
async def test_odds_request_passes_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "onexbet_enabled", True)
    provider = make_provider()
    paths: list[tuple[str, dict[str, Any]]] = []

    async def fake_get(path: str, params: dict[str, Any] | None = None) -> Any:
        paths.append((path, params or {}))
        return MARKETS_V2

    provider._get = fake_get  # type: ignore[method-assign]
    prematch = await provider.get_odds("12345")
    live = await provider.get_live_odds("12345")
    assert prematch and live
    assert paths[0] == ("/matches/12345/markets_v2", {"mode": "line"})
    assert paths[1] == ("/matches/12345/markets_v2", {"mode": "live"})


@pytest.mark.asyncio
async def test_upcoming_skips_unsupported_sport(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "onexbet_enabled", True)
    provider = make_provider()

    async def refuse(*_a: Any, **_k: Any) -> Any:  # pragma: no cover
        raise AssertionError("для неподдерживаемого спорта запросов быть не должно")

    provider._get = refuse  # type: ignore[method-assign]
    assert await provider.get_upcoming("boxing", date.today()) == []


@pytest.mark.asyncio
async def test_upcoming_uses_configured_leagues(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "onexbet_enabled", True)
    monkeypatch.setattr(settings, "onexbet_leagues", '{"cs2": [111]}')
    monkeypatch.setattr(settings, "onexbet_sport_ids", '{"cs2": 555}')
    provider = make_provider()
    starts = datetime.now(timezone.utc) + timedelta(hours=5)
    requests: list[tuple[str, dict[str, Any]]] = []

    async def fake_get(path: str, params: dict[str, Any] | None = None) -> Any:
        requests.append((path, params or {}))
        return [
            {
                "id": "987",
                "team1": "Vitality",
                "team2": "Spirit",
                "dateStart": str(int(starts.timestamp())),
            }
        ]

    provider._get = fake_get  # type: ignore[method-assign]
    matches = await provider.get_upcoming("cs2", starts.date())
    assert len(matches) == 1
    assert matches[0].home_team == "Vitality"
    assert requests[0] == ("/matches", {"league_id": 111, "sport_id": 555})


@pytest.mark.asyncio
async def test_budget_blocks_requests_when_exhausted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "onexbet_enabled", True)
    monkeypatch.setattr(settings, "onexbet_monthly_limit", 1)
    provider = make_provider()
    calls = 0

    async def fake_json(url: str, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        return {"status": "success", "data": []}

    provider.get_json = fake_json  # type: ignore[method-assign]
    await provider._get("/sports")
    await provider._get("/sports")  # бюджет исчерпан
    assert calls == 1
    assert provider.budget_remaining == 0


@pytest.mark.asyncio
async def test_auth_error_disables_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "onexbet_enabled", True)
    provider = make_provider()
    calls = 0

    async def fake_json(url: str, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        raise SourceError("1xbet: HTTP 403 на https://1xbet-api.p.rapidapi.com/sports (subscribe)")

    provider.get_json = fake_json  # type: ignore[method-assign]
    assert await provider._get("/sports") is None
    assert provider.available is False
    assert "RapidAPI" in (provider.disabled_reason or "")
    assert await provider._get("/sports") is None
    assert calls == 1
