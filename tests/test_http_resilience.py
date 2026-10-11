"""Устойчивость HTTP-слоя: классификация сбоев, circuit breaker, деградация источников.

Это регрессия на прод-лог, где:
  * Liquipedia 404 опрашивалась каждые 45 секунд бесконечно (128+ сбоев подряд);
  * OpenDota-таймаут валил лайв-тик сырым httpx-трейсбеком;
  * анализатор ходил за кэфами в матчи, которые всё равно отсекал whitelist.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from app.config import settings
from app.db.models import Match as MatchRow, MatchStatus
from app.pipeline.analyzer import analyze_match
from app.sources.base import (
    BaseHttpClient,
    CircuitBreaker,
    CircuitBreakerOpen,
    SourceError,
    SourceUnavailable,
)
from app.sources.opendota import OpenDotaProvider
from app.sources.theoddsapi import TheOddsApiProvider


# --------------------------------------------------------------------------- #
# Circuit breaker: временные и детерминированные сбои
# --------------------------------------------------------------------------- #
def test_transient_failures_open_breaker_for_short_time() -> None:
    breaker = CircuitBreaker("api", failure_threshold=3, recovery_time_sec=5.0, permanent_recovery_time_sec=600.0)
    for _ in range(3):
        breaker.record_failure()
    assert breaker.state == "OPEN"
    assert breaker.cooldown_sec == 5.0
    with pytest.raises(CircuitBreakerOpen):
        breaker.before_request()


def test_permanent_failure_opens_breaker_for_long_time() -> None:
    breaker = CircuitBreaker(
        "api", failure_threshold=6, recovery_time_sec=5.0,
        permanent_recovery_time_sec=600.0, permanent_weight=3,
    )
    breaker.record_failure(permanent=True)  # вес 3 < 6
    assert breaker.state == "CLOSED"
    breaker.record_failure(permanent=True)  # 3 + 3 = 6 → OPEN
    assert breaker.state == "OPEN"
    assert breaker.cooldown_sec == 600.0
    # Пробный запрос раньше времени не пропускаем.
    with pytest.raises(CircuitBreakerOpen) as exc:
        breaker.before_request()
    assert "600" in str(exc.value)
    # ...а после «прошествия» паузы — переходим в HALF_OPEN.
    breaker.opened_at -= 601
    breaker.before_request()
    assert breaker.state == "HALF_OPEN"
    breaker.record_success()
    assert breaker.state == "CLOSED"
    assert breaker.cooldown_sec == breaker.recovery_time_sec


# --------------------------------------------------------------------------- #
# BaseHttpClient: ошибки наружу уходят как SourceError
# --------------------------------------------------------------------------- #
async def _client_with_handler(handler: Any) -> BaseHttpClient:
    client = BaseHttpClient(source_name="test", base_url="https://api.test")
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client


@pytest.mark.asyncio
async def test_timeout_becomes_source_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "http_backoff_base_sec", 0.01)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("timed out", request=request)

    client = await _client_with_handler(handler)
    with pytest.raises(SourceUnavailable) as exc:
        await client.request("GET", "/live", retries=2)
    message = str(exc.value)
    assert "сеть/таймаут" in message and "ReadTimeout" in message
    assert calls == 2  # ретраи сработали
    assert client.circuit_breaker.consecutive_failures == 1
    await client.aclose()


@pytest.mark.asyncio
async def test_http_404_is_not_retried_and_weighs_as_permanent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "http_backoff_base_sec", 0.01)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(404, text="<html>Error - Liquipedia DB</html>", request=request)

    client = await _client_with_handler(handler)
    with pytest.raises(SourceError) as exc:
        await client.request("GET", "/query", retries=3)
    assert not isinstance(exc.value, SourceUnavailable)  # это не «источник недоступен»
    assert "404" in str(exc.value)
    assert calls == 1  # детерминированную ошибку не повторяем
    assert client.circuit_breaker.consecutive_failures == settings.http_breaker_permanent_weight
    await client.aclose()


@pytest.mark.asyncio
async def test_http_503_is_retried_and_weighs_as_transient(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "http_backoff_base_sec", 0.01)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, text="upstream down", request=request)

    client = await _client_with_handler(handler)
    with pytest.raises(SourceUnavailable):
        await client.request("GET", "/x", retries=3)
    assert calls == 3
    assert client.circuit_breaker.consecutive_failures == 1
    await client.aclose()


@pytest.mark.asyncio
async def test_open_breaker_does_not_touch_network() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={}, request=request)

    client = await _client_with_handler(handler)
    client.circuit_breaker.state = "OPEN"
    client.circuit_breaker.opened_at = 10**9  # монотонное время «в будущем» → пауза не истекла
    client.circuit_breaker.cooldown_sec = 600.0
    with pytest.raises(CircuitBreakerOpen):
        await client.request("GET", "/x")
    assert calls == 0
    await client.aclose()


# --------------------------------------------------------------------------- #
# OpenDota: лайв-тик не должен «висеть»
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_opendota_live_uses_short_timeout_and_one_retry() -> None:
    provider = OpenDotaProvider()
    calls: list[tuple[str, dict[str, Any]]] = []

    async def fake_get_json(url: str, **kwargs: Any) -> Any:
        calls.append((url, kwargs))
        return []

    provider.get_json = fake_get_json  # type: ignore[method-assign]
    await provider.get_live_matches("dota2")
    live_calls = [kwargs for url, kwargs in calls if url.endswith("/live")]
    assert len(live_calls) == 1  # /live опрашивается один раз
    assert live_calls[0]["retries"] == settings.opendota_live_retries == 1
    assert live_calls[0]["timeout"] == settings.opendota_live_timeout_sec


# --------------------------------------------------------------------------- #
# Анализатор: whitelist проверяется ДО похода за кэфами
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_analyzer_skips_odds_for_league_outside_whitelist(session: Any, sport_id: int) -> None:
    match = MatchRow(
        sport_id=sport_id,
        ext_id="mma-1",
        league="AHL Hockey",
        starts_at=datetime.now(timezone.utc) + timedelta(hours=3),
        status=MatchStatus.SCHEDULED,
    )
    session.add(match)
    await session.flush()

    calls: list[int] = []

    async def refresh_match(*_args: Any, **_kwargs: Any) -> list[Any]:
        calls.append(1)
        return []

    providers = SimpleNamespace(aggregator=SimpleNamespace(refresh_match=refresh_match))
    report = await analyze_match(session, match, pass_no=2, providers=providers)

    assert report.status == "screener_skip"
    assert "whitelist" in report.reason
    assert calls == []  # в источники кэфов не ходили


# --------------------------------------------------------------------------- #
# TheOddsApi: пауза после исчерпания лимита free-тарифа
# --------------------------------------------------------------------------- #
def _enabled_odds_api(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "the_odds_api_enabled", True)
    monkeypatch.setattr(settings, "the_odds_api_key", "test-key")


@pytest.mark.asyncio
async def test_theoddsapi_pauses_after_out_of_usage_credits(monkeypatch: pytest.MonkeyPatch) -> None:
    _enabled_odds_api(monkeypatch)
    provider = TheOddsApiProvider()
    calls = 0

    async def fake_request(method: str, url: str, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        raise SourceError(
            "theoddsapi: HTTP 401 на https://api.the-odds-api.com/v4/sports/soccer/odds: "
            '{"message":"Usage quota has been reached","error_code":"OUT_OF_USAGE_CREDITS"}'
        )

    provider.request = fake_request  # type: ignore[method-assign]
    with pytest.raises(SourceError):
        await provider._get("/sports/soccer/odds", {})
    assert calls == 1
    assert provider.quota_exhausted is True
    assert provider.available is False  # источник выпадает из опроса — 401 больше не летит

    with pytest.raises(SourceError):
        await provider._get("/sports/soccer/odds", {})
    assert calls == 1  # в сеть больше не ходим, ошибка сразу локальная


@pytest.mark.asyncio
async def test_theoddsapi_pauses_when_remaining_header_is_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    _enabled_odds_api(monkeypatch)
    provider = TheOddsApiProvider()

    async def fake_request(method: str, url: str, **kwargs: Any) -> httpx.Response:
        return httpx.Response(
            200,
            json=[],
            headers={"x-requests-remaining": "0", "x-requests-used": "500"},
            request=httpx.Request("GET", url),
        )

    provider.request = fake_request  # type: ignore[method-assign]
    assert await provider._get("/sports", {}) == []
    assert provider.quota_exhausted is True
    assert provider.quota_info()["quota_exhausted_until"] is not None


@pytest.mark.asyncio
async def test_theoddsapi_skips_unsupported_sports_without_request() -> None:
    provider = TheOddsApiProvider(api_key="test-key")

    async def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("для неподдерживаемого спорта запрос уходить не должен")

    provider.get_json = refuse  # type: ignore[method-assign]
    assert await provider.get_upcoming("dota2", date.today()) == []
    assert await provider.get_upcoming("cs2", date.today()) == []


@pytest.mark.asyncio
async def test_theoddsapi_unknown_event_scan_is_negative_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    _enabled_odds_api(monkeypatch)
    provider = TheOddsApiProvider()
    scanned = 0

    async def fake_get(path: str, params: dict[str, Any]) -> Any:
        nonlocal scanned
        scanned += 1
        return []

    provider._get = fake_get  # type: ignore[method-assign]
    assert await provider._resolve_sport_key("no-such-event") is None
    first_scan = scanned
    assert first_scan > 0  # первый раз спорты сканировались
    assert await provider._resolve_sport_key("no-such-event") is None
    assert scanned == first_scan  # повторный скан запрещён негативным кэшем
