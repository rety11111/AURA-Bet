"""Базовые контракты источников данных (Модуль 1).

Здесь живут:
  * pydantic-модели обмена (Match / Odd / Injury / Lineup / FormEntry / H2H / ...);
  * BaseHttpClient — httpx + tenacity + ротация User-Agent + паузы (для скрейпинга);
  * TTLCache — кэш в памяти с TTL, чтобы не жечь лимиты free-tier API;
  * абстракции OddsProvider / StatsProvider / EsportsProvider — ровно те методы,
    которые описаны в ТЗ. Конкретные источники наследуются и реализуют их.

Правило деградации: любая ошибка источника превращается в SourceUnavailable /
SourceError, логируется и НЕ роняет процесс (см. вызовы в pipeline/collector.py).
"""

from __future__ import annotations

import abc
import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timezone
from typing import Any, Generic, TypeVar

import httpx
from loguru import logger
from pydantic import BaseModel, Field
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.config import settings

T = TypeVar("T")


# --------------------------------------------------------------------------- #
# Исключения
# --------------------------------------------------------------------------- #
class SourceError(Exception):
    """Любая ошибка источника (парсинг, схема ответа, 4xx и т.п.)."""


class SourceUnavailable(SourceError):
    """Источник недоступен/не сконфигурирован (нет URL, нет ключа, 5xx после ретраев)."""


# --------------------------------------------------------------------------- #
# Модели обмена данными
# --------------------------------------------------------------------------- #
class Odd(BaseModel):
    """Один коэффициент рынка у одного источника."""

    market: str                      # 1x2 | totals | handicap | double_chance
    selection: str                    # home | draw | away | over | under | home_handicap | away_handicap | 1x | 12 | x2
    price: float
    source: str
    line: float | None = None         # 2.5 для тотала, -1.5 для форы
    captured_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def key(self) -> tuple[str, str, float | None]:
        return (self.market, self.selection, self.line)


class Match(BaseModel):
    """Матч (событие) в нормализованном виде."""

    ext_id: str
    sport: str                        # football | hockey | basketball | tennis | mma | boxing | dota2 | cs2
    league: str
    home_team: str
    away_team: str
    starts_at: datetime
    source: str
    league_tier: str | None = None
    status: str = "scheduled"         # scheduled | live | finished | postponed | cancelled
    home_score: int | None = None
    away_score: int | None = None
    live_stage: str | None = None     # "драфт", "карта 2, раунд 7", "минута 11" ...
    is_live: bool = False
    extra: dict[str, Any] = Field(default_factory=dict)

    def label(self) -> str:
        return f"{self.home_team} — {self.away_team}"


class Injury(BaseModel):
    team: str
    player: str
    status: str = "unknown"           # out | doubtful | day-to-day | returned
    reason: str | None = None
    source: str | None = None
    importance: str | None = None     # ключевой игрок / ролевой — заполняется, если источник даёт


class Lineup(BaseModel):
    team: str
    players: list[str] = Field(default_factory=list)
    confirmed: bool = False
    goalkeeper: str | None = None
    formation: str | None = None
    source: str | None = None


class FormEntry(BaseModel):
    date: datetime | None = None
    opponent: str | None = None
    is_home: bool | None = None
    goals_for: int | None = None
    goals_against: int | None = None
    xg: float | None = None
    xga: float | None = None
    result: str | None = None         # W | D | L
    competition: str | None = None


class H2HRecord(BaseModel):
    date: datetime | None = None
    home_team: str | None = None
    away_team: str | None = None
    home_score: int | None = None
    away_score: int | None = None
    competition: str | None = None


class ScheduleDensity(BaseModel):
    matches_last_7_days: int = 0
    matches_next_7_days: int = 0
    rest_days: int | None = None
    back_to_back: bool = False
    three_in_four: bool = False
    notes: str | None = None


class TeamXgStats(BaseModel):
    """xG-статистика футбольной/хоккейной команды (для Пуассона)."""

    team: str
    matches: int = 0
    xg_for_per_game: float | None = None
    xg_against_per_game: float | None = None
    xg_for_home: float | None = None
    xg_against_home: float | None = None
    xg_for_away: float | None = None
    xg_against_away: float | None = None
    goals_for_per_game: float | None = None
    goals_against_per_game: float | None = None
    source: str | None = None


class BasketballTeamStats(BaseModel):
    """Баскетбольные показатели: темп и эффективность."""

    team: str
    pace: float | None = None
    ortg: float | None = None
    drtg: float | None = None
    games: int = 0
    rest_days: int | None = None
    back_to_back: bool = False
    source: str | None = None


class LivePlayerStat(BaseModel):
    """Игрок в лайв-матче Dota 2 (OpenDota /api/live)."""

    account_id: int | None = None
    name: str | None = None
    hero: str | None = None
    net_worth: int | None = None
    xp: int | None = None
    last_hits: int | None = None
    deaths: int | None = None
    level: int | None = None


class EsportsLiveMatch(BaseModel):
    """Лайв-матч киберспорта (Dota 2 / CS2) в нормализованном виде."""

    ext_id: str
    sport: str                          # dota2 | cs2
    league: str
    league_tier: str | None = None
    team_a: str
    team_b: str
    source: str
    match_time: datetime | None = None
    series_score: tuple[int, int] | None = None
    map_number: int | None = None
    map_name: str | None = None
    game_minute: int | None = None
    game_time_sec: int | None = None
    rounds_a: int | None = None
    rounds_b: int | None = None
    draft_a: list[str] = Field(default_factory=list)
    draft_b: list[str] = Field(default_factory=list)
    draft_bans_a: list[str] = Field(default_factory=list)
    draft_bans_b: list[str] = Field(default_factory=list)
    players_a: list[LivePlayerStat] = Field(default_factory=list)
    players_b: list[LivePlayerStat] = Field(default_factory=list)
    net_worth_a: int | None = None
    net_worth_b: int | None = None
    xp_a: int | None = None
    xp_b: int | None = None
    is_draft_complete: bool = False
    stage: str | None = None            # человекочитаемая стадия для сообщения
    extra: dict[str, Any] = Field(default_factory=dict)


class MapStats(BaseModel):
    team: str
    map_name: str
    matches: int = 0
    win_rate: float | None = None
    round_win_rate: float | None = None
    source: str | None = None


# --------------------------------------------------------------------------- #
# TTL-кэш в памяти
# --------------------------------------------------------------------------- #
class TTLCache:
    """Простой потокобезопасный (в рамках event loop) TTL-кэш."""

    def __init__(self, ttl_sec: float, maxsize: int = 2048) -> None:
        self.ttl = ttl_sec
        self.maxsize = maxsize
        self._data: dict[str, tuple[float, Any]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def get(self, key: str) -> Any | None:
        item = self._data.get(key)
        if not item:
            return None
        expires_at, value = item
        if expires_at < time.monotonic():
            self._data.pop(key, None)
            return None
        return value

    def set(self, key: str, value: Any, ttl_sec: float | None = None) -> None:
        if len(self._data) >= self.maxsize:
            # Выкидываем самый старый элемент (FIFO по вставке dict).
            self._data.pop(next(iter(self._data)), None)
        self._data[key] = (time.monotonic() + (ttl_sec if ttl_sec is not None else self.ttl), value)

    def invalidate(self, key: str) -> None:
        self._data.pop(key, None)

    def clear(self) -> None:
        self._data.clear()

    async def get_or_set(self, key: str, factory: Callable[[], Awaitable[T]], ttl_sec: float | None = None) -> T:
        cached = self.get(key)
        if cached is not None:
            return cached  # type: ignore[return-value]
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            cached = self.get(key)
            if cached is not None:
                return cached  # type: ignore[return-value]
            value = await factory()
            self.set(key, value, ttl_sec)
            return value


# --------------------------------------------------------------------------- #
# HTTP-клиент
# --------------------------------------------------------------------------- #
class BaseHttpClient:
    """Обёртка над httpx.AsyncClient: таймауты, ретраи с экспоненциальным бэкоффом,
    ротация User-Agent и вежливые паузы для скрейпинга.

    Все внешние вызовы в проекте обязаны иметь таймаут — здесь он задаётся
    settings.http_timeout_sec.
    """

    source_name = "http"

    def __init__(
        self,
        source_name: str,
        base_url: str = "",
        headers: dict[str, str] | None = None,
        politeness: bool = False,
        timeout: float | None = None,
    ) -> None:
        self.source_name = source_name
        self.base_url = base_url.rstrip("/")
        self.headers = dict(headers or {})
        self.politeness = politeness
        self.timeout = timeout or settings.http_timeout_sec
        self._client: httpx.AsyncClient | None = None
        self._ua_index = random.randrange(max(1, len(settings.user_agents) or 1))

    # ------------------------------------------------------------- lifecycle
    def _build_client(self) -> httpx.AsyncClient:
        timeout = httpx.Timeout(self.timeout, connect=settings.http_connect_timeout_sec)
        limits = httpx.Limits(max_connections=10, max_keepalive_connections=5)
        return httpx.AsyncClient(
            timeout=timeout,
            limits=limits,
            follow_redirects=True,
            headers={"Accept": "application/json, text/plain, */*", **self.headers},
        )

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = self._build_client()
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = dict(extra or {})
        uas = settings.user_agents
        if self.politeness and uas:
            headers["User-Agent"] = uas[self._ua_index % len(uas)]
            self._ua_index += 1
            headers.setdefault("Accept-Language", "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7")
        return headers

    async def _polite_sleep(self) -> None:
        if self.politeness:
            await asyncio.sleep(random.uniform(settings.scrape_delay_min_sec, settings.scrape_delay_max_sec))

    # -------------------------------------------------------------- requests
    async def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
        retries: int | None = None,
        polite: bool | None = None,
    ) -> httpx.Response:
        full_url = url if url.startswith("http") else f"{self.base_url}{url}"
        attempts = retries or settings.http_max_retries
        polite = self.politeness if polite is None else polite
        last_error: Exception | None = None

        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(attempts),
            wait=wait_exponential(multiplier=settings.http_backoff_base_sec, max=settings.http_backoff_max_sec),
            retry=retry_if_exception_type((httpx.TransportError, httpx.TimeoutException, SourceUnavailable)),
            reraise=True,
        ):
            with attempt:
                await self._polite_sleep() if polite else None
                try:
                    response = await self.client.request(
                        method,
                        full_url,
                        params=params,
                        headers=self._headers(headers),
                        json=json_body,
                    )
                except (httpx.TransportError, httpx.TimeoutException) as exc:
                    last_error = exc
                    logger.warning("{}: сеть/таймаут {} — попытка {}", self.source_name, full_url, attempt.retry_state.attempt_number)
                    raise
                if response.status_code in (429, 500, 502, 503, 504):
                    last_error = SourceUnavailable(f"{self.source_name}: HTTP {response.status_code}")
                    logger.warning(
                        "{}: HTTP {} на {} — ретрай {}", self.source_name, response.status_code, full_url,
                        attempt.retry_state.attempt_number,
                    )
                    raise SourceUnavailable(str(last_error))
                if response.status_code >= 400:
                    raise SourceError(f"{self.source_name}: HTTP {response.status_code} на {full_url}: {response.text[:200]}")
                return response
        # Сюда попадаем только если AsyncRetrying исчерпал попытки и reraise не сработал.
        raise SourceUnavailable(f"{self.source_name}: не удалось выполнить запрос ({last_error})")

    async def get_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        polite: bool | None = None,
    ) -> Any:
        response = await self.request("GET", url, params=params, headers=headers, polite=polite)
        try:
            return response.json()
        except ValueError as exc:
            raise SourceError(f"{self.source_name}: ответ не JSON ({response.text[:200]})") from exc

    async def get_text(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        polite: bool | None = None,
    ) -> str:
        response = await self.request("GET", url, params=params, headers=headers, polite=polite)
        return response.text


# --------------------------------------------------------------------------- #
# Абстракции источников (ровно интерфейсы из ТЗ)
# --------------------------------------------------------------------------- #
class OddsProvider(BaseHttpClient, abc.ABC):
    """Источник коэффициентов."""

    provider_name = "odds"

    @property
    def available(self) -> bool:
        return bool(self.base_url)

    @abc.abstractmethod
    async def get_upcoming(self, sport: str, day: date) -> list[Match]:
        """Прематчевые события спорта на дату."""

    @abc.abstractmethod
    async def get_odds(self, match_id: str) -> list[Odd]:
        """Коэффициенты конкретного прематчевого матча (1X2/П1П2, тоталы, форы)."""

    @abc.abstractmethod
    async def get_live_odds(self, match_id: str) -> list[Odd]:
        """Лайв-коэффициенты матча (включая киберспорт: счёт серии/карты в Match.extra)."""


class StatsProvider(BaseHttpClient, abc.ABC):
    """Источник статистики (травмы, составы, форма, H2H, плотность календаря)."""

    provider_name = "stats"
    sport = "generic"

    @abc.abstractmethod
    async def get_injuries(self, team: str, **kwargs: Any) -> list[Injury]:
        ...

    @abc.abstractmethod
    async def get_predicted_lineups(self, team: str, **kwargs: Any) -> Lineup | None:
        ...

    @abc.abstractmethod
    async def get_recent_form(self, team: str, n: int = 5, **kwargs: Any) -> list[FormEntry]:
        ...

    @abc.abstractmethod
    async def get_h2h(self, home_team: str, away_team: str, **kwargs: Any) -> list[H2HRecord]:
        ...

    @abc.abstractmethod
    async def get_schedule_density(self, team: str, **kwargs: Any) -> ScheduleDensity:
        ...


class EsportsProvider(BaseHttpClient, abc.ABC):
    """Источник киберспортивных лайв-данных."""

    provider_name = "esports"

    @abc.abstractmethod
    async def get_live_matches(self, sport: str) -> list[EsportsLiveMatch]:
        ...

    @abc.abstractmethod
    async def get_draft(self, match_id: str) -> tuple[list[str], list[str], list[str], list[str]]:
        """(пики A, пики B, баны A, баны B)."""

    @abc.abstractmethod
    async def get_map_stats(self, team: str, map_name: str) -> MapStats | None:
        ...

    @abc.abstractmethod
    async def get_tournament_tier(self, tournament: str) -> str | None:
        ...


# --------------------------------------------------------------------------- #
# Утилиты
# --------------------------------------------------------------------------- #
def to_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def safe_int(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(float(value))
    except (TypeError, ValueError):
        return None


def safe_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None


def deep_find_list(payload: Any, candidate_keys: tuple[str, ...], max_depth: int = 6) -> list[Any]:
    """Ищет в произвольном JSON первый список по одному из ключей-кандидатов.

    Нужно для парсеров букмекеров: структура ответа может отличаться от описанной,
    поэтому код не падает, а пытается найти массив событий/коэффициентов в известных местах.
    """
    queue: list[tuple[Any, int]] = [(payload, 0)]
    while queue:
        node, depth = queue.pop(0)
        if depth > max_depth:
            continue
        if isinstance(node, dict):
            for key in candidate_keys:
                value = node.get(key)
                if isinstance(value, list) and value:
                    return value
            for value in node.values():
                if isinstance(value, (dict, list)):
                    queue.append((value, depth + 1))
        elif isinstance(node, list):
            for value in node:
                if isinstance(value, (dict, list)):
                    queue.append((value, depth + 1))
    return []


def deep_find_value(payload: Any, candidate_keys: tuple[str, ...], max_depth: int = 6) -> Any:
    """Возвращает первое скалярное значение по ключу-кандидату (BFS)."""
    queue: list[tuple[Any, int]] = [(payload, 0)]
    while queue:
        node, depth = queue.pop(0)
        if depth > max_depth:
            continue
        if isinstance(node, dict):
            for key in candidate_keys:
                if key in node and not isinstance(node[key], (dict, list)):
                    return node[key]
            for value in node.values():
                if isinstance(value, (dict, list)):
                    queue.append((value, depth + 1))
        elif isinstance(node, list):
            for value in node:
                if isinstance(value, (dict, list)):
                    queue.append((value, depth + 1))
    return None


def pick(node: dict[str, Any], keys: tuple[str, ...], default: Any = None) -> Any:
    """Первое непустое значение из словаря по списку ключей-кандидатов."""
    for key in keys:
        if key in node and node[key] not in (None, "", [], {}):
            return node[key]
    return default
