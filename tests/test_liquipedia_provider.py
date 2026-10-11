"""Тесты провайдера Liquipedia на реальном контракте LPDB API v3.

Проверяем то, на чём спотыкался прод:
  * без ключа провайдер выключен (LPDB v3 не работает без `Authorization: Apikey ...`);
  * запросы идут на эндпоинты по типу данных (`/match`, `/tournament`), а не на
    несуществующий `/api/v3/query` (он возвращал HTML 404 каждые 45 секунд);
  * условия собираются в нотации LPDB (`[[finished::0]] AND [[date::>...]]`);
  * есть часовой бюджет запросов (free-tier ~60/час) и «тихое» поведение при сбоях;
  * парсинг match2opponents/match2games соответствует схеме LPDB.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.config import settings
from app.sources.base import SourceError
from app.sources.liquipedia import (
    HourlyBudget,
    LiquipediaProvider,
    _best_name_match,
    _game_scores,
    _opponents,
    lpdb_condition,
    lpdb_conditions,
)


def make_provider(**kwargs: Any) -> LiquipediaProvider:
    """Провайдер с «рабочим» ключом и заглушкой-клиентом (сети нет)."""
    provider = LiquipediaProvider(api_key="test-key", **kwargs)
    provider.headers["User-Agent"] = "BetSignalsBot/1.0 (contact: test@aura.bet)"
    return provider


# --------------------------------------------------------------------------- #
# Условия LPDB
# --------------------------------------------------------------------------- #
def test_lpdb_conditions_syntax() -> None:
    assert lpdb_condition("finished", 0) == "[[finished::0]]"
    assert lpdb_condition("date", "2026-01-01 00:00:00", ">") == "[[date::>2026-01-01 00:00:00]]"
    assert lpdb_condition("type", "online", "!=") == "[[type::!online]]"
    # Значения не должны «разрывать» условия своими скобками.
    assert lpdb_condition("opponent", "Team]] Liquid") == "[[opponent::Team Liquid]]"
    assert (
        lpdb_conditions([lpdb_condition("finished", 0), lpdb_condition("date", "2026-01-01", ">")])
        == "[[finished::0]] AND [[date::>2026-01-01]]"
    )


# --------------------------------------------------------------------------- #
# Доступность
# --------------------------------------------------------------------------- #
def test_provider_disabled_without_api_key() -> None:
    provider = LiquipediaProvider(api_key="")
    provider.headers["User-Agent"] = "BetSignalsBot/1.0 (contact: test@aura.bet)"
    assert provider.available is False
    assert provider.disabled_reason is not None
    assert "LIQUIPEDIA_API_KEY" in (provider.disabled_reason or "")


def test_provider_disabled_with_placeholder_user_agent() -> None:
    provider = LiquipediaProvider(api_key="test-key")
    provider.headers["User-Agent"] = "BetSignalsBot/1.0 (contact: you@example.com)"
    assert provider.available is False
    assert "LIQUIPEDIA_USER_AGENT" in (provider.disabled_reason or "")


def test_provider_enabled_with_key_and_contact() -> None:
    provider = make_provider()
    assert provider.available is True
    assert provider.headers["Authorization"] == "Apikey test-key"
    # База — LPDB v3, а не удалённый /query.
    assert provider.base_url.endswith("/api/v3")
    assert not provider.base_url.endswith("/query")


# --------------------------------------------------------------------------- #
# Запросы
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_query_hits_typed_endpoint_with_lpdb_params() -> None:
    provider = make_provider()
    calls: list[tuple[str, dict[str, Any]]] = []

    async def fake_get_json(url: str, **kwargs: Any) -> Any:
        calls.append((url, kwargs.get("params") or {}))
        return {"result": [{"match2id": "abc_1"}]}

    provider.get_json = fake_get_json  # type: ignore[method-assign]
    rows = await provider.query(
        "match",
        wiki="dota2",
        fields=("match2id", "date", "match2opponents"),
        conditions=lpdb_conditions([lpdb_condition("finished", 0)]),
        order="date asc",
        limit=60,
    )
    assert rows == [{"match2id": "abc_1"}]
    assert len(calls) == 1
    url, params = calls[0]
    assert url == "/match"
    assert params["wiki"] == "dota2"
    assert params["query"] == "match2id,date,match2opponents"
    assert params["conditions"] == "[[finished::0]]"
    assert params["order"] == "date asc"
    assert params["limit"] == "60"


@pytest.mark.asyncio
async def test_query_is_cached_and_budget_limited() -> None:
    provider = make_provider(hourly_limit=2)
    calls = 0

    async def fake_get_json(url: str, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        return {"result": []}

    provider.get_json = fake_get_json  # type: ignore[method-assign]
    await provider.query("match", wiki="dota2", limit=10)
    await provider.query("match", wiki="dota2", limit=10)  # тот же запрос — из кэша
    await provider.query("tournament", wiki="dota2", limit=10)  # другой — новый запрос
    await provider.query("team", wiki="dota2", limit=10)  # бюджет (2) исчерпан
    assert calls == 2
    assert provider._budget.used == 2


def test_hourly_budget_window() -> None:
    budget = HourlyBudget(limit=2, window_sec=10.0)
    assert budget.take(now=0.0) is True
    assert budget.take(now=1.0) is True
    assert budget.take(now=2.0) is False  # окно 0..10 уже заполнено
    assert budget.take(now=11.0) is True  # первый запрос вышел из окна


@pytest.mark.asyncio
async def test_auth_error_disables_provider_once() -> None:
    provider = make_provider()
    calls = 0

    async def fake_get_json(url: str, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        raise SourceError("liquipedia: HTTP 403 на /match: forbidden")

    provider.get_json = fake_get_json  # type: ignore[method-assign]
    assert await provider.query("match", wiki="dota2") == []
    assert provider.available is False
    assert provider.disabled_reason is not None
    # Второй вызов не должен снова стучаться в API.
    assert await provider.query("match", wiki="dota2") == []
    assert calls == 1


@pytest.mark.asyncio
async def test_api_error_in_body_is_not_cached() -> None:
    provider = make_provider()
    calls = 0

    async def fake_get_json(url: str, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        return {"error": ["Invalid condition"], "result": []}

    provider.get_json = fake_get_json  # type: ignore[method-assign]
    assert await provider.query("match", wiki="dota2") == []
    assert await provider.query("match", wiki="dota2") == []  # сбой не кэшируем
    assert calls == 2
    assert provider.available is True  # не ошибка авторизации — провайдер жив


# --------------------------------------------------------------------------- #
# Парсинг расписания
# --------------------------------------------------------------------------- #
MATCH_ROW = {
    "match2id": "esl_one_bangkok_2026_R01-M001",
    "date": "2030-01-01 12:00:00",
    "finished": 0,
    "bestof": 3,
    "tournament": "ESL One Bangkok 2026",
    "liquipediatier": 1,
    "liquipediatiertype": "Major",
    "match2opponents": [
        {"match2opponentid": 1, "type": "team", "name": "Team Liquid", "template": "team liquid", "score": 1},
        {"match2opponentid": 2, "type": "team", "name": "PARIVISION", "template": "parivision", "score": 0},
    ],
    "match2games": [
        {"match2gameid": 1, "map": "Nuke", "scores": {"1": 13, "2": 9}, "winner": 1},
        {"match2gameid": 2, "map": "Mirage", "scores": {"1": 8, "2": 13}, "winner": 2},
        {"match2gameid": 3, "map": "Inferno", "scores": [10, 13], "winner": 2},
    ],
}


def test_opponents_and_game_scores_parsing() -> None:
    opponents = _opponents(MATCH_ROW)
    assert [item["name"] for item in opponents] == ["Team Liquid", "PARIVISION"]
    assert [item["index"] for item in opponents] == [1, 2]
    assert _game_scores(MATCH_ROW["match2games"][0]) == (13, 9)
    assert _game_scores(MATCH_ROW["match2games"][2]) == (10, 13)


@pytest.mark.asyncio
async def test_get_upcoming_matches_parses_rows() -> None:
    provider = make_provider()

    async def fake_get_json(url: str, **kwargs: Any) -> Any:
        return {"result": [MATCH_ROW]}

    provider.get_json = fake_get_json  # type: ignore[method-assign]
    matches = await provider.get_upcoming_matches("dota2", days=3650)
    assert len(matches) == 1
    match = matches[0]
    assert (match.team_a, match.team_b) == ("Team Liquid", "PARIVISION")
    assert match.league == "ESL One Bangkok 2026"
    assert match.league_tier == "S-Tier"
    assert match.ext_id == "esl_one_bangkok_2026_R01-M001"
    assert match.source == "liquipedia"


@pytest.mark.asyncio
async def test_get_upcoming_matches_ignores_unsupported_sport() -> None:
    provider = make_provider()

    async def fake_get_json(url: str, **kwargs: Any) -> Any:  # pragma: no cover
        raise AssertionError("запрос не должен уходить для неподдерживаемого спорта")

    provider.get_json = fake_get_json  # type: ignore[method-assign]
    assert await provider.get_upcoming_matches("lol") == []


# --------------------------------------------------------------------------- #
# Статистика по картам и tier
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_get_map_stats_counts_games() -> None:
    provider = make_provider()

    async def fake_get_json(url: str, **kwargs: Any) -> Any:
        return {"result": [MATCH_ROW]}

    provider.get_json = fake_get_json  # type: ignore[method-assign]
    stats = await provider.get_map_stats("Team Liquid", "Nuke")
    assert stats is not None
    assert stats.matches == 1
    assert stats.win_rate == 1.0
    assert stats.round_win_rate == round(13 / 22, 4)  # 13 раундов взято, 9 отдано


@pytest.mark.asyncio
async def test_tier_lookup_falls_back_to_catalog() -> None:
    provider = make_provider()

    async def fake_get_json(url: str, **kwargs: Any) -> Any:
        params = kwargs.get("params") or {}
        if "[[name::" in str(params.get("conditions", "")):
            return {"result": []}  # точного совпадения нет — как с именами из OpenDota
        return {
            "result": [
                {"name": "ESL One Bangkok 2026", "liquipediatier": 1, "liquipediatiertype": ""},
                {"name": "Some Weekly Cup", "liquipediatier": 4, "liquipediatiertype": "Weekly"},
            ]
        }

    provider.get_json = fake_get_json  # type: ignore[method-assign]
    assert await provider.get_tournament_tier("ESL One 2026 Bangkok") == "S-Tier"


def test_best_name_match_threshold() -> None:
    rows = [{"name": "ESL One Bangkok 2026", "liquipediatier": 1}]
    assert _best_name_match("ESL One Bangkok 2026", rows) is not None
    assert _best_name_match("Ничего общего", rows) is None


def test_default_api_url_points_to_lpdb_base() -> None:
    assert settings.liquipedia_api_url == "https://api.liquipedia.net/api/v3"


def test_old_query_url_is_normalized_with_warning() -> None:
    """Старый LIQUIPEDIA_API_URL из .env не должен воскрешать HTML-404 эндпоинт."""
    provider = LiquipediaProvider(api_key="test-key", api_url="https://api.liquipedia.net/api/v3/query")
    provider.headers["User-Agent"] = "BetSignalsBot/1.0 (contact: test@aura.bet)"
    assert provider.base_url == "https://api.liquipedia.net/api/v3"
    assert provider.available is True
