"""LPDB v3: ресурсные URL, авторизация и тихая деградация."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx

from app.sources.liquipedia import LiquipediaProvider

UA = "BetSignalsBot/1.0 (contact: maintainer@mysite.org)"


async def test_v3_query_uses_resource_route_and_auth() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"result": [{"match2id": "m1"}], "warnings": []})

    provider = LiquipediaProvider(
        api_url="https://api.liquipedia.net/api/v3/query", user_agent=UA, api_key="test-key"
    )
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(respond), headers=provider.headers)
    try:
        rows = await provider.query("dota2", "match", conditions="[[finished::0]]", order="date ASC")
        assert rows == [{"match2id": "m1"}]
        assert str(requests[0].url).startswith("https://api.liquipedia.net/api/v3/match?")
        assert requests[0].url.params["conditions"] == "[[finished::0]]"
        assert requests[0].url.params["wiki"] == "dota2"
        assert requests[0].url.params["order"] == "date ASC"
        assert "tables" not in requests[0].url.params
        assert requests[0].headers["authorization"] == "Apikey test-key"
        assert requests[0].headers["user-agent"] == UA
    finally:
        await provider.aclose()


async def test_missing_key_disables_provider_without_request() -> None:
    provider = LiquipediaProvider(user_agent=UA, api_key="")
    assert not provider.available
    assert await provider.query("dota2", "match") == []
    assert provider._client is None


async def test_404_disables_provider_instead_of_retrying_forever() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(404, text="<html>Not Found</html>")

    provider = LiquipediaProvider(user_agent=UA, api_key="test-key")
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(respond), headers=provider.headers)
    try:
        assert await provider.query("dota2", "match") == []
        assert not provider.available
        assert await provider.query("counterstrike", "match") == []
        assert len(requests) == 1
    finally:
        await provider.aclose()


async def test_404_during_repeated_tier_checks_stops_further_requests() -> None:
    """100 live-турниров не должны генерировать 100 повторных 404 после первой ошибки."""
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(404, text="<html>Not Found</html>")

    provider = LiquipediaProvider(user_agent=UA, api_key="test-key")
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(respond), headers=provider.headers)
    try:
        for index in range(100):
            assert await provider.get_tournament_tier(f"League {index}") is None
        assert not provider.available
        assert len(requests) == 1
    finally:
        await provider.aclose()


async def test_upcoming_and_tournament_use_v3_resources() -> None:
    paths: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/tournament"):
            return httpx.Response(200, json={"result": [{"name": "Major", "liquipediatier": "1"}]})
        return httpx.Response(200, json={"result": []})

    provider = LiquipediaProvider(user_agent=UA, api_key="test-key")
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(respond), headers=provider.headers)
    try:
        assert await provider.get_upcoming_matches("cs2") == []
        assert await provider.get_tournament_tier("Major") == "S-Tier"
        assert paths == ["/api/v3/match", "/api/v3/tournament"]
    finally:
        await provider.aclose()


async def test_v3_match_parses_nested_opponents_and_games() -> None:
    start = datetime.now(UTC) + timedelta(hours=2)
    match = {
        "match2id": "match-1", "date": start.strftime("%Y-%m-%d %H:%M:%S"),
        "tournament": "Major", "liquipediatier": "1",
        "match2opponents": [{"name": "Alpha"}, {"name": "Beta"}],
    }
    finished = {
        **match, "match2games": [{"map": "Mirage", "score1": 13, "score2": 9, "winner": 1}],
    }

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("conditions") == "[[finished::1]]":
            return httpx.Response(200, json={"result": [finished]})
        return httpx.Response(200, json={"result": [match]})

    provider = LiquipediaProvider(user_agent=UA, api_key="test-key")
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(respond), headers=provider.headers)
    try:
        upcoming = await provider.get_upcoming_matches("cs2")
        assert len(upcoming) == 1
        assert (upcoming[0].team_a, upcoming[0].team_b, upcoming[0].league_tier) == ("Alpha", "Beta", "S-Tier")
        stats = await provider.get_map_stats("Alpha", "Mirage")
        assert stats is not None
        assert (stats.matches, stats.win_rate, stats.round_win_rate) == (1, 1.0, 0.5909)
    finally:
        await provider.aclose()
