"""Liquipedia (LPDB API v3) — киберспорт: расписание, tier турниров, статистика по картам.

ЧЕСТНО ПРО API (важно — из-за этого сыпался прод-лог):
  * У Liquipedia есть официальный REST API — LiquipediaDB (LPDB) v3:

        GET https://api.liquipedia.net/api/v3/<datatype>

    где <datatype> ∈ {match, tournament, team, player, series, placement, transfer, ...}.
    Эндпоинта `/api/v3/query` НЕ существует: сервер отдаёт на него HTML-страницу
    «Error - Liquipedia DB» с кодом 404 (именно это и было видно в логах каждые 45 секунд).
  * Ключ ОБЯЗАТЕЛЕН: заголовок `Authorization: Apikey <LIQUIPEDIA_API_KEY>`. Ключ
    запрашивается на https://liquipedia.net/api (выдаётся не автоматически). Без ключа
    API отвечает `{"error": ["API key \"\" is not valid."]}`, поэтому провайдер без
    ключа просто выключен — иначе мы бы жгли лимиты и лог впустую.
  * Параметры запроса (см. https://liquipedia.net/commons/Help:LiquipediaDB):
      wiki       — dota2 | counterstrike (несколько вики — через «|»);
      query      — список полей через запятую (например, match2id,date,match2opponents);
      conditions — условия в нотации LPDB: `[[date::>2026-01-01]] AND [[finished::0]]`,
                   операторы `::` (равно), `::>` (больше), `::<` (меньше), `::!` (не равно).
                   Для матчей есть спец-условие `[[opponent::Team]]` — «любой оппонент
                   матча с таким именем»;
      order      — `date asc`; limit/offset — пагинация (не больше 1000 записей за запрос).
  * Free-tier: ~60 запросов в час на ключ. Поэтому здесь есть скользящий часовой бюджет
    (LIQUIPEDIA_HOURLY_LIMIT, дефолт 50), кэш (CACHE_TTL_LIQUIPEDIA_SEC) и «длинный»
    circuit breaker: детерминированные сбои (401/403/404) не долбятся каждую минуту.
  * Liquipedia НЕ отдаёт live-счёт матчей. Лайв Dota 2 (драфт, экономика) — OpenDota,
    CS2-лайв — Winline/BetBoom live. `get_live_matches` здесь строит «недавно начавшиеся»
    матчи по дате.

Схема таблиц: https://liquipedia.net/commons/Help:LiquipediaDB/Match
"""

from __future__ import annotations

import json
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Sequence

from loguru import logger
from rapidfuzz import fuzz

from app.config import settings
from app.sources.base import (
    EsportsLiveMatch,
    EsportsProvider,
    MapStats,
    SourceError,
    TTLCache,
    safe_int,
)

WIKI_BY_SPORT = {"dota2": "dota2", "cs2": "counterstrike"}
# Один запрос по нескольким вики: LPDB поддерживает перечисление через «|».
ALL_WIKIS = "|".join(dict.fromkeys(WIKI_BY_SPORT.values()))
# Соответствие числового tier Liquipedia → строке, которую понимает скринер.
TIER_LABELS = {"1": "S-Tier", "2": "A-Tier", "3": "B-Tier", "4": "C-Tier", "5": "D-Tier"}
NON_TIER_TYPES = ("qualifier", "monthly", "weekly", "showmatch", "show match", "charity", "unranked")
# Поля матча, которые просим у LPDB. Вложенные таблицы (match2opponents/match2games)
# возвращаются только если их явно указать в query.
MATCH_FIELDS: tuple[str, ...] = (
    "match2id",
    "date",
    "finished",
    "bestof",
    "tournament",
    "series",
    "liquipediatier",
    "liquipediatiertype",
    "match2opponents",
)
TOURNAMENT_FIELDS: tuple[str, ...] = ("name", "liquipediatier", "liquipediatiertype", "startdate", "enddate")
# TTL кэша ответов LPDB по типам данных (защита free-tier: 60 запросов/час).
QUERY_TTL_SEC: dict[str, float] = {
    "match": 15 * 60,
    "tournament": 12 * 3600,
}
DEFAULT_QUERY_TTL_SEC = 15 * 60
# Насколько похожими должны быть названия турниров, чтобы считать их одним и тем же.
NAME_MATCH_THRESHOLD = 85.0


def _parse_datetime(raw: Any) -> datetime | None:
    if raw in (None, ""):
        return None
    if isinstance(raw, (int, float)):
        try:
            return datetime.fromtimestamp(float(raw), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(raw).strip()
    if text.isdigit():
        return _parse_datetime(int(text))
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d", "%Y-%m-%d %H:%M"):
        try:
            parsed = datetime.strptime(text.replace("Z", "+0000"), fmt)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    logger.debug("liquipedia: не удалось разобрать дату {!r}", raw)
    return None


def _lpdb_datetime(moment: datetime) -> str:
    """Дата в формате LPDB-условий (`YYYY-MM-DD HH:MM:SS`, UTC)."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def lpdb_condition(field: str, value: Any, op: str = "=") -> str:
    """Одно условие LPDB: [[field::value]], [[field::>value]], [[field::!value]] и т.д."""
    operator = {"=": "", "==": "", ">": ">", "<": "<", "!=": "!", "!": "!"}.get(op, "")
    text = str(value).replace("[[", "").replace("]]", "").strip()
    return f"[[{field}::{operator}{text}]]"


def lpdb_conditions(parts: Iterable[str]) -> str:
    """Условия LPDB объединяются словом AND."""
    return " AND ".join(part for part in parts if part)


def _json_list(value: Any) -> list[Any]:
    """LPDB отдаёт вложенные массивы как list либо как объект с ключами-индексами."""
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        def sort_key(item: tuple[Any, Any]) -> tuple[int, int, str]:
            key = str(item[0])
            return (0, int(key), "") if key.isdigit() else (1, 0, key)

        return [item for _, item in sorted(value.items(), key=sort_key)]
    return []


def _opponents(row: dict[str, Any]) -> list[dict[str, Any]]:
    """Оппоненты матча: имя + 1-based индекс (match2opponentid), как в LPDB."""
    opponents: list[dict[str, Any]] = []
    for position, item in enumerate(_json_list(row.get("match2opponents")), start=1):
        if not isinstance(item, dict):
            continue
        name = item.get("name") or item.get("opponentname") or item.get("template") or item.get("opponenttemplate")
        if not name:
            continue
        index = safe_int(item.get("match2opponentid")) or position
        opponents.append({
            "index": index,
            "name": str(name).strip(),
            "template": str(item.get("template") or "").strip(),
            "score": safe_int(item.get("score")),
            "type": str(item.get("type") or "").strip(),
        })
    return opponents


def _games(row: dict[str, Any]) -> list[dict[str, Any]]:
    """Карты/игры серии (match2games)."""
    return [game for game in _json_list(row.get("match2games")) if isinstance(game, dict)]


def _game_scores(game: dict[str, Any]) -> tuple[int | None, int | None]:
    """Счёт карты: scores[1] — счёт первого оппонента, scores[2] — второго."""
    scores = game.get("scores")
    values: list[int | None] = []
    if isinstance(scores, dict):
        values = [safe_int(scores.get("1", scores.get(1))), safe_int(scores.get("2", scores.get(2)))]
    elif isinstance(scores, (list, tuple)):
        values = [safe_int(item) for item in scores[:2]]
    if len(values) < 2 or (values[0] is None and values[1] is None):
        values = [safe_int(game.get("score1")), safe_int(game.get("score2"))]
    if len(values) < 2:
        return None, None
    return values[0], values[1]


def _tier_code(raw: Any) -> str:
    """Нормализует liquipediatier (в API это число: 1..5, иногда строка)."""
    number = safe_int(raw)
    if number is not None:
        return str(number)
    return str(raw or "").strip()


def _tier_from_rows(rows: Sequence[dict[str, Any]]) -> str | None:
    """Tier/тип турнира из строки таблицы tournament (или match: те же поля)."""
    for row in rows:
        tiertype = str(row.get("liquipediatiertype") or "").strip()
        if tiertype and any(marker in tiertype.lower() for marker in NON_TIER_TYPES):
            return tiertype
        tier = _tier_code(row.get("liquipediatier"))
        if tier in TIER_LABELS:
            return TIER_LABELS[tier]
        if tiertype:
            return tiertype
    return None


def _normalize_name(name: str) -> str:
    cleaned = "".join(char.lower() if char.isalnum() else " " for char in name)
    return " ".join(cleaned.split())


def _best_name_match(target: str, rows: Sequence[dict[str, Any]]) -> dict[str, Any] | None:
    """Ищет турнир по названию среди строк каталога (имена у источников пишутся по-разному)."""
    normalized = _normalize_name(target)
    if not normalized:
        return None
    best_score = 0.0
    best_row: dict[str, Any] | None = None
    for row in rows:
        candidate = _normalize_name(str(row.get("name") or ""))
        if not candidate:
            continue
        score = 100.0 if candidate == normalized else float(
            max(fuzz.partial_ratio(normalized, candidate), fuzz.token_set_ratio(normalized, candidate))
        )
        if score > best_score:
            best_score, best_row = score, row
    if best_row is None or best_score < NAME_MATCH_THRESHOLD:
        if best_row is not None:
            logger.debug(
                "liquipedia: турнир '{}' не сопоставлен (лучший вариант '{}', score={:.1f} < {})",
                target, best_row.get("name"), best_score, NAME_MATCH_THRESHOLD,
            )
        return None
    return best_row


class HourlyBudget:
    """Скользящее окно запросов в час — защита free-tier Liquipedia (~60 запросов/час)."""

    def __init__(self, limit: int, window_sec: float = 3600.0) -> None:
        self.limit = max(1, int(limit))
        self.window_sec = float(window_sec)
        self._events: deque[float] = deque()
        self._last_request: float | None = None

    def take(self, now: float | None = None) -> bool:
        moment = now if now is not None else time.monotonic()
        while self._events and moment - self._events[0] > self.window_sec:
            self._events.popleft()
        if len(self._events) >= self.limit:
            return False
        self._events.append(moment)
        self._last_request = moment
        return True

    @property
    def used(self) -> int:
        return len(self._events)


class LiquipediaProvider(EsportsProvider):
    """LPDB API v3: расписания, tier турниров, статистика по картам."""

    provider_name = "liquipedia"

    def __init__(
        self,
        api_url: str | None = None,
        user_agent: str | None = None,
        api_key: str | None = None,
        hourly_limit: int | None = None,
    ) -> None:
        ua = (user_agent or settings.liquipedia_user_agent or "").strip()
        headers = {"Accept": "application/json"}
        if ua:
            headers["User-Agent"] = ua
        key = (api_key if api_key is not None else settings.liquipedia_api_key or "").strip()
        self.api_key = key
        if key:
            # Официальный формат LPDB v3 (см. https://liquipedia.net/api).
            headers["Authorization"] = f"Apikey {key}"
        base_url = (api_url or settings.liquipedia_api_url).rstrip("/")
        if base_url.endswith("/query"):
            # Совместимость со старым .env: /api/v3/query у Liquipedia не существует,
            # а в проде он годами давал HTML-404 и циклил circuit breaker. Срезаем хвост.
            base_url = base_url[: -len("/query")]
            logger.warning(
                "liquipedia: LIQUIPEDIA_API_URL указывает на несуществующий эндпоинт /query — "
                "используем базу LPDB v3: {}",
                base_url,
            )
        super().__init__(
            source_name="liquipedia",
            base_url=base_url,
            headers=headers,
        )
        self._cache = TTLCache(ttl_sec=settings.cache_ttl_liquipedia_sec)
        self._budget = HourlyBudget(
            hourly_limit if hourly_limit is not None else settings.liquipedia_hourly_limit
        )
        self._disabled_reason: str | None = None
        self._last_failure: str | None = None

    @property
    def available(self) -> bool:
        if not self.base_url or self._disabled_reason:
            return False
        if not self.api_key:
            self._disable(
                "нет LIQUIPEDIA_API_KEY — LPDB v3 без ключа отвечает ошибкой авторизации "
                "(ключ запрашивается на https://liquipedia.net/api)"
            )
            return False
        if "example.com" in (self.headers.get("User-Agent") or ""):
            self._disable(
                "LIQUIPEDIA_USER_AGENT не настроен (осталась заглушка example.com) — "
                "Liquipedia блокирует запросы без честного контакта (см. SETUP.md)"
            )
            return False
        return True

    # ------------------------------------------------------------- диагностика
    @property
    def disabled_reason(self) -> str | None:
        """Почему провайдер выключен (для /health и scripts/check_sources.py)."""
        return self._disabled_reason

    def _disable(self, reason: str) -> None:
        """Выключает провайдера до рестарта процесса (повторять бессмысленно)."""
        if self._disabled_reason == reason:
            return
        self._disabled_reason = reason
        logger.warning("liquipedia: провайдер отключён до рестарта — {}", reason)

    def _fail(self, message: str) -> None:
        """Сбой одной причины логируем один раз; повторы — в DEBUG (иначе спам в логах)."""
        if self._last_failure == message:
            logger.debug("liquipedia: повтор сбоя — {}", message)
            return
        self._last_failure = message
        logger.warning("liquipedia: {}", message)

    # ------------------------------------------------------------------- query
    async def query(
        self,
        endpoint: str,
        *,
        wiki: str,
        fields: Sequence[str] | None = None,
        conditions: str | None = None,
        order: str | None = None,
        limit: int = 50,
        offset: int = 0,
        groupby: str | None = None,
        cache_ttl_sec: float | None = None,
    ) -> list[dict[str, Any]]:
        """Запрос к LPDB v3: GET {base}/{endpoint}?wiki=...&query=...&conditions=...

        `endpoint` — тип данных LPDB: match / tournament / team / player / series / ...
        (`/query` у Liquipedia не существует — только эти типы). Битый запрос не роняет
        сервис: возвращаем пустой список, причина уходит в лог один раз.
        """
        if not self.available:
            return []
        limit = max(1, min(int(limit), 1000))  # LPDB: максимум 1000 записей за запрос
        params: dict[str, Any] = {"wiki": wiki, "limit": str(limit), "offset": str(offset)}
        if fields:
            params["query"] = ",".join(fields)
        if conditions:
            params["conditions"] = conditions
        if order:
            params["order"] = order
        if groupby:
            params["groupby"] = groupby

        cache_key = "q:" + json.dumps([endpoint, params], ensure_ascii=False, sort_keys=True)
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached  # type: ignore[return-value]
        rows = await self._fetch(endpoint, params)
        if rows is None:
            return []  # сбой не кэшируем — попробуем в следующий раз
        ttl = cache_ttl_sec if cache_ttl_sec is not None else QUERY_TTL_SEC.get(endpoint, DEFAULT_QUERY_TTL_SEC)
        self._cache.set(cache_key, rows, ttl_sec=ttl)
        return rows

    async def _fetch(self, endpoint: str, params: dict[str, Any]) -> list[dict[str, Any]] | None:
        if not self._budget.take():
            self._fail(
                f"часовой бюджет запросов исчерпан (LIQUIPEDIA_HOURLY_LIMIT={self._budget.limit}) — "
                "новые запросы до конца окна не отправляем"
            )
            return None
        try:
            # politeness=False: LPDB требует НАШ User-Agent с контактом, а ротация
            # «браузерных» UA (politeness=True) его бы перетёрла и получила блокировку.
            payload = await self.get_json(f"/{endpoint}", params=params)
        except SourceError as exc:
            message = str(exc)
            if "401" in message or "403" in message:
                self._disable(
                    f"LPDB отклонил ключ (проверьте LIQUIPEDIA_API_KEY): {message[:200]}"
                )
            else:
                self._fail(f"запрос /{endpoint} (wiki={params.get('wiki')}) не удался: {message[:200]}")
            return None
        except Exception as exc:  # pragma: no cover — страховка от неожиданных ошибок
            self._fail(f"неожиданная ошибка запроса /{endpoint}: {exc}")
            return None

        if isinstance(payload, dict):
            error = payload.get("error")
            if error:
                text = "; ".join(str(item) for item in error) if isinstance(error, list) else str(error)
                if "key" in text.lower():
                    self._disable(f"LPDB отклонил ключ: {text[:200]}")
                else:
                    self._fail(f"API вернул ошибку на /{endpoint}: {text[:200]}")
                return None
            if payload.get("warning"):
                logger.debug("liquipedia: warning от API: {}", str(payload["warning"])[:200])
            result = payload.get("result")
            if isinstance(result, list):
                self._last_failure = None
                return [row for row in result if isinstance(row, dict)]
            self._last_failure = None
            return []
        if isinstance(payload, list):
            self._last_failure = None
            return [row for row in payload if isinstance(row, dict)]
        self._fail(f"неожиданный формат ответа /{endpoint}: {type(payload).__name__}")
        return None

    # ------------------------------------------------------------ расписания
    async def get_upcoming_matches(self, sport: str, days: int = 3) -> list[EsportsLiveMatch]:
        """Запланированные матчи на ближайшие `days` дней (finished::0)."""
        wiki = WIKI_BY_SPORT.get(sport.lower())
        if not wiki:
            logger.debug("liquipedia: спорт '{}' не поддерживается (только dota2/cs2)", sport)
            return []
        now = datetime.now(timezone.utc)
        horizon = now + timedelta(days=days)

        async def factory() -> list[EsportsLiveMatch]:
            rows = await self.query(
                "match",
                wiki=wiki,
                fields=MATCH_FIELDS,
                conditions=lpdb_conditions([
                    lpdb_condition("finished", 0),
                    lpdb_condition("date", _lpdb_datetime(now - timedelta(hours=6)), ">"),
                ]),
                order="date asc",
                limit=60,
            )
            matches: list[EsportsLiveMatch] = []
            for row in rows:
                moment = _parse_datetime(row.get("date"))
                if moment is None or not (now - timedelta(hours=6) <= moment <= horizon):
                    continue
                opponents = _opponents(row)
                if len(opponents) < 2:
                    continue
                matches.append(
                    EsportsLiveMatch(
                        ext_id=str(row.get("match2id") or f"{wiki}:{opponents[0]['name']}:{opponents[1]['name']}"),
                        sport=sport.lower(),
                        league=str(row.get("tournament") or row.get("series") or "Unknown"),
                        league_tier=_tier_from_rows([row]),
                        team_a=opponents[0]["name"],
                        team_b=opponents[1]["name"],
                        source=self.provider_name,
                        match_time=moment,
                        series_score=None,
                        extra={"bestof": row.get("bestof"), "wiki": wiki, "match2id": row.get("match2id")},
                    )
                )
            logger.info("liquipedia: {} → {} матчей на ближайшие {} дн.", wiki, len(matches), days)
            return matches

        return await self._cache.get_or_set(
            f"upcoming:{wiki}:{days}", factory, ttl_sec=settings.cache_ttl_liquipedia_sec
        )

    async def get_live_matches(self, sport: str) -> list[EsportsLiveMatch]:
        """Liquipedia не является live-источником.

        Возвращаем матчи, начавшиеся не позднее 4 часов назад и не помеченные завершёнными.
        Реальный лайв (драфт, счёт, экономика) берём из OpenDota, CS2-лайв — из Winline.
        """
        wiki = WIKI_BY_SPORT.get(sport.lower())
        if not wiki:
            return []
        now = datetime.now(timezone.utc)

        async def factory() -> list[EsportsLiveMatch]:
            rows = await self.query(
                "match",
                wiki=wiki,
                fields=MATCH_FIELDS,
                conditions=lpdb_conditions([
                    lpdb_condition("finished", 0),
                    lpdb_condition("date", _lpdb_datetime(now - timedelta(hours=6)), ">"),
                ]),
                order="date asc",
                limit=30,
            )
            matches: list[EsportsLiveMatch] = []
            for row in rows:
                moment = _parse_datetime(row.get("date"))
                if moment is None or not (now - timedelta(hours=4) <= moment <= now + timedelta(minutes=15)):
                    continue
                opponents = _opponents(row)
                if len(opponents) < 2:
                    continue
                score_a, score_b = opponents[0]["score"], opponents[1]["score"]
                matches.append(
                    EsportsLiveMatch(
                        ext_id=str(row.get("match2id") or f"{wiki}:{opponents[0]['name']}:{opponents[1]['name']}"),
                        sport=sport.lower(),
                        league=str(row.get("tournament") or "Unknown"),
                        league_tier=_tier_from_rows([row]),
                        team_a=opponents[0]["name"],
                        team_b=opponents[1]["name"],
                        source=self.provider_name,
                        match_time=moment,
                        series_score=(score_a, score_b) if score_a is not None and score_b is not None else None,
                        extra={
                            "live_source": "liquipedia (только факт начала матча, без счёта по картам)",
                            "wiki": wiki,
                        },
                    )
                )
            return matches

        return await self._cache.get_or_set(
            f"live:{wiki}", factory, ttl_sec=settings.cache_ttl_esports_sec
        )

    async def get_draft(self, match_id: str) -> tuple[list[str], list[str], list[str], list[str]]:
        """Liquipedia драфты не отдаёт — драфт берётся из OpenDota (Dota 2).

        Возвращаем пустые списки, чтобы вызывающий код не падал и продолжил работу
        без драфт-контекста (см. live/esports_worker.py: драфт — обязательное условие
        для сигнала по Dota, поэтому без OpenDota лайв-сигналов по Dota не будет).
        """
        logger.debug("liquipedia: get_draft({}) — драфт доступен только через OpenDota", match_id)
        return [], [], [], []

    async def get_team_roster(self, sport: str, team: str) -> list[str] | None:
        """Состав команды из LPDB (`/squadplayer`), если ключ есть и данные доступны.

        LPDB не гарантирует актуальный ростер (в вики попадают не все составы), поэтому
        результат мягкий: список ников либо None. Ошибки не роняют анализ.
        """
        wiki = WIKI_BY_SPORT.get((sport or "").lower())
        name = (team or "").strip()
        if not wiki or not name:
            return None

        async def factory() -> list[str] | None:
            rows = await self.query(
                "squadplayer",
                wiki=wiki,
                fields=("id", "pagename", "name", "position", "status"),
                conditions=lpdb_conditions([lpdb_condition("team", name)]),
                limit=20,
            )
            players: list[str] = []
            for row in rows:
                if str(row.get("status") or "").lower() in ("former", "inactive"):
                    continue
                player = str(row.get("id") or row.get("pagename") or row.get("name") or "").strip()
                if player:
                    players.append(player)
            return players or None

        return await self._cache.get_or_set(
            f"roster:{wiki}:{name.lower()}", factory, ttl_sec=settings.cache_ttl_liquipedia_sec
        )

    # -------------------------------------------------------- статистика карт
    async def get_map_stats(self, team: str, map_name: str) -> MapStats | None:
        """Винрейт команды по конкретной карте (CS2: Nuke/Mirage/..., Dota: карта = матч).

        Берём матчи команды за последний год (`[[opponent::Team]]` — спец-условие LPDB
        «любой оппонент матча»), внутри каждой серии смотрим match2games и оставляем
        игры на нужной карте. `scores[1]` — счёт первого оппонента, `scores[2]` — второго.
        """
        team_key = (team or "").strip().lower()
        map_key = (map_name or "").strip().lower()
        if not team_key or not map_key:
            return None

        async def factory() -> MapStats | None:
            since = _lpdb_datetime(datetime.now(timezone.utc) - timedelta(days=365))
            rows = await self.query(
                "match",
                wiki=ALL_WIKIS,
                fields=("match2id", "date", "liquipediatier", "match2opponents", "match2games"),
                conditions=lpdb_conditions([
                    lpdb_condition("opponent", team),
                    lpdb_condition("date", since, ">"),
                ]),
                order="date desc",
                limit=100,
            )
            wins = losses = rounds_won = rounds_lost = 0
            for row in rows:
                opponents = _opponents(row)
                team_index = next(
                    (item["index"] for item in opponents if team_key in item["name"].lower()),
                    None,
                )
                if team_index is None:
                    continue
                for game in _games(row):
                    game_map = str(game.get("map") or "").strip().lower()
                    if not game_map or (map_key not in game_map and game_map not in map_key):
                        continue
                    score_a, score_b = _game_scores(game)
                    if score_a is None or score_b is None:
                        winner = safe_int(game.get("winner"))
                        if winner in (1, 2):
                            if winner == team_index:
                                wins += 1
                            else:
                                losses += 1
                        continue
                    team_score, opp_score = (score_a, score_b) if team_index == 1 else (score_b, score_a)
                    rounds_won += team_score
                    rounds_lost += opp_score
                    if team_score > opp_score:
                        wins += 1
                    elif team_score < opp_score:
                        losses += 1
            games = wins + losses
            if games == 0:
                logger.info(
                    "liquipedia: статистика по карте '{}' для '{}' не найдена (нет игр за год)",
                    map_name, team,
                )
                return None
            return MapStats(
                team=team,
                map_name=map_name,
                matches=games,
                win_rate=round(wins / games, 4),
                round_win_rate=(
                    round(rounds_won / (rounds_won + rounds_lost), 4) if (rounds_won + rounds_lost) else None
                ),
                source="liquipedia:match2games",
            )

        return await self._cache.get_or_set(
            f"map:{team_key}:{map_key}", factory, ttl_sec=settings.cache_ttl_liquipedia_sec
        )

    # ------------------------------------------------------------ tier турнира
    async def get_tournament_tier(self, tournament: str, sport: str | None = None) -> str | None:
        """Tier турнира: 'S-Tier' / 'A-Tier' / ... либо тип ('Qualifier' и т.п.).

        `sport` принимается для совместимости с Cito (там каталог событий свой для
        каждой игры); LPDB ищет сразу по обеим вики, поэтому параметр не используется.
        """
        name = (tournament or "").strip()
        if not name:
            return None

        async def factory() -> str | None:
            # 1) точное совпадение имени (самый дешёвый запрос);
            rows = await self.query(
                "tournament",
                wiki=ALL_WIKIS,
                fields=TOURNAMENT_FIELDS,
                conditions=lpdb_condition("name", name),
                limit=5,
            )
            tier = _tier_from_rows(rows)
            if tier:
                return tier
            # 2) фолбэк: каталог турниров за год + нечёткое сравнение имён
            #    (источники пишут названия по-разному: «ESL One Bangkok» vs «ESL One 2026 Bangkok»).
            matched = _best_name_match(name, await self._tournament_catalog())
            if matched is None:
                return None
            return _tier_from_rows([matched])

        return await self._cache.get_or_set(
            f"tier:{name.lower()}", factory, ttl_sec=settings.cache_ttl_liquipedia_sec
        )

    async def _tournament_catalog(self) -> list[dict[str, Any]]:
        """Каталог турниров за последний год (один запрос, кэш 12 часов)."""
        since = (datetime.now(timezone.utc) - timedelta(days=365)).strftime("%Y-%m-%d")
        return await self.query(
            "tournament",
            wiki=ALL_WIKIS,
            fields=TOURNAMENT_FIELDS,
            conditions=lpdb_condition("startdate", since, ">"),
            order="startdate desc",
            limit=500,
        )
