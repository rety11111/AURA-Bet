"""Cito API — киберспорт CS2 и Dota 2 (расписание, tier турниров, статистика карт, ростеры).

Зачем: Liquipedia требует ключ, выдаваемый вручную, и её free-tier прямо запрещает
беттинг-проекты. Cito даёт те же данные (расписание, tier события, винрейт команды по
карте, ростер) без этих ограничений.

Контракт API (проверено по документации https://citoapi.com/docs/api/cs2/):
  * база `https://api.citoapi.com/api/v1`, ключ — заголовок `x-api-key`;
  * ответ всегда `{"success": bool, "data": ..., "meta": {...}}`; при ошибке —
    `{"success": false, "error": "..."}`;
  * CS2:  `/cs2/matches/upcoming`, `/cs2/matches/today`, `/cs2/live`,
          `/cs2/teams/{slug}/stats` (там же `mapWinRates`), `/cs2/teams/{slug}/roster`,
          `/cs2/events/upcoming`, `/cs2/events/recent`;
  * Dota: `/dota2/matches/upcoming`, `/dota2/matches/live`, `/dota2/matches/{id}/draft`.

⚠️ ЛИМИТЫ — free-план даёт всего 500 запросов/месяц. Поэтому:
  * каждый вызов идёт через кэш (расписание 30 мин, каталог событий 12 ч, команды 12 ч);
  * есть месячный бюджет (CITO_MONTHLY_LIMIT, дефолт 450): исчерпали — источник сам
    замолкает до конца окна, вместо того чтобы получить 429 на каждом тике;
  * лайв-опрос (`/cs2/live` каждые 45 с) по умолчанию ВЫКЛЮЧЕН (CITO_LIVE_ENABLED=false):
    45-секундный поллинг съедает месячный лимит за пару суток. Включайте осознанно.

Схема строки матча (CS2/Dota, проверено по живым примерам в документации):
    id, eventId, eventName, eventSlug, stageName, startsAt, bestOf, status
    team1Id/team2Id, team1Name/team2Name, team1Score/team2Score, event{tier, prestigeScore}
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

from loguru import logger
from rapidfuzz import fuzz

from app.config import settings
from app.sources.base import (
    EsportsLiveMatch,
    EsportsProvider,
    MapStats,
    Match,
    MonthlyBudget,
    SourceError,
    TTLCache,
    safe_int,
)

# Виды спорта, которые отдаёт Cito (префиксы путей API).
SPORT_PREFIX = {"cs2": "cs2", "dota2": "dota2"}
# Буквенный tier события Cito → словарь, который понимает скринер («S-Tier», «A-Tier», ...).
TIER_LABELS = {"s": "S-Tier", "a": "A-Tier", "b": "B-Tier", "c": "C-Tier", "d": "D-Tier"}
# Если буква tier не заполнена — оцениваем уровень по prestigeScore (0..100).
PRESTIGE_BANDS: tuple[tuple[int, str], ...] = ((85, "S-Tier"), (70, "A-Tier"), (50, "B-Tier"), (0, "C-Tier"))
NAME_MATCH_THRESHOLD = 85.0
MATCH_FIELDS = (
    "id",
    "eventId",
    "eventName",
    "stageName",
    "startsAt",
    "bestOf",
    "status",
    "team1Name",
    "team2Name",
    "team1Score",
    "team2Score",
    "event",
)


def _parse_datetime(raw: Any) -> datetime | None:
    if raw in (None, ""):
        return None
    if isinstance(raw, (int, float)):
        try:
            return datetime.fromtimestamp(float(raw), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(raw).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _normalize_name(name: str) -> str:
    cleaned = "".join(char.lower() if char.isalnum() else " " for char in name)
    return " ".join(cleaned.split())


def team_slug(name: str) -> str:
    """«Natus Vincere» → «natus-vincere» (Cito принимает id или slug)."""
    cleaned = "".join(char.lower() if char.isalnum() else "-" for char in (name or "").strip())
    return "-".join(part for part in cleaned.split("-") if part)


def tier_label(tier: Any, prestige: Any = None) -> str | None:
    """Буквенный tier Cito («S»/«A»/...) → «S-Tier», иначе оценка по prestigeScore."""
    letter = str(tier or "").strip().lower()
    if letter in TIER_LABELS:
        return TIER_LABELS[letter]
    score = safe_int(prestige)
    if score is not None:
        for threshold, label in PRESTIGE_BANDS:
            if score >= threshold:
                return label
    return None


class CitoEsportsProvider(EsportsProvider):
    """Cito API: CS2 и Dota 2 — расписание, tier, карты, ростеры."""

    provider_name = "cito"

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        monthly_limit: int | None = None,
        **kwargs: Any,
    ) -> None:
        self.api_key = (api_key if api_key is not None else settings.cito_api_key or "").strip()
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["x-api-key"] = self.api_key
        super().__init__(
            source_name=self.provider_name,
            base_url=(base_url or settings.cito_api_base).rstrip("/"),
            headers=headers,
            **kwargs,
        )
        self._cache = TTLCache(ttl_sec=settings.cito_cache_ttl_sec, maxsize=512)
        self._budget = MonthlyBudget(
            monthly_limit if monthly_limit is not None else settings.cito_monthly_limit
        )
        self._disabled_reason: str | None = None
        self._last_failure: str | None = None

    # ------------------------------------------------------------- доступность
    @property
    def available(self) -> bool:
        if self._disabled_reason:
            return False
        if not self.api_key:
            self._disable(
                "нет CITO_API_KEY — Cito отдаёт данные только с ключом (берётся на https://citoapi.com)"
            )
            return False
        return True

    @property
    def disabled_reason(self) -> str | None:
        return self._disabled_reason

    @property
    def budget_remaining(self) -> int:
        return self._budget.remaining

    def _disable(self, reason: str) -> None:
        """Выключает источник до рестарта: 401/403 и отсутствие ключа не «самоисправятся»."""
        if self._disabled_reason == reason:
            return
        self._disabled_reason = reason
        logger.warning("cito: источник отключён до рестарта — {}", reason)

    def _fail(self, message: str) -> None:
        """Повторяющиеся сбои логируем один раз (иначе спам на каждом тике)."""
        if self._last_failure == message:
            logger.debug("cito: повтор сбоя — {}", message)
            return
        self._last_failure = message
        logger.warning("cito: {}", message)

    # ------------------------------------------------------------------ запросы
    async def _request(self, path: str, params: dict[str, Any] | None = None) -> Any:
        if not self.available:
            return None
        if not self._budget.take():
            self._fail(
                f"месячный бюджет запросов исчерпан (CITO_MONTHLY_LIMIT={self._budget.limit}) — "
                "новые запросы до конца окна не отправляем"
            )
            return None
        try:
            payload = await self.get_json(f"{self.base_url}{path}", params=params or {})
        except SourceError as exc:
            message = str(exc)
            if "401" in message or "403" in message:
                self._disable(f"Cito отклонил ключ (проверьте CITO_API_KEY): {message[:200]}")
            elif "404" in message:
                self._fail(f"эндпоинт не найден: {path} (проверьте схему Cito)")
            else:
                self._fail(f"запрос {path} не удался: {message[:200]}")
            return None
        except Exception as exc:  # noqa: BLE001 — источник не должен ронять пайплайн
            self._fail(f"неожиданная ошибка запроса {path}: {exc}")
            return None

        if isinstance(payload, dict):
            if payload.get("success") is False:
                text = str(payload.get("error") or "неизвестная ошибка")
                if "key" in text.lower() or "unauthor" in text.lower():
                    self._disable(f"Cito отклонил ключ: {text[:200]}")
                else:
                    self._fail(f"API вернул ошибку на {path}: {text[:200]}")
                return None
            self._last_failure = None
            return payload.get("data")
        self._last_failure = None
        return payload

    async def _cached(self, key: str, ttl_sec: float, path: str, params: dict[str, Any] | None = None) -> Any:
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        data = await self._request(path, params)
        if data is None:
            return None  # сбой не кэшируем — попробуем в следующий раз
        self._cache.set(key, data, ttl_sec=ttl_sec)
        return data

    def _sport_prefix(self, sport: str) -> str | None:
        return SPORT_PREFIX.get((sport or "").lower())

    # ---------------------------------------------------------------- расписание
    async def get_upcoming_matches(self, sport: str, days: int = 2) -> list[Match]:
        """Запланированные матчи на ближайшие `days` дней (нормализованные Match)."""
        prefix = self._sport_prefix(sport)
        if not prefix:
            logger.debug("cito: спорт '{}' не поддерживается (только cs2/dota2)", sport)
            return []
        rows = await self._cached(
            f"upcoming:{prefix}:{days}",
            settings.cito_cache_ttl_sec,
            f"/{prefix}/matches/upcoming",
            {"limit": 100},
        )
        if not isinstance(rows, list):
            return []
        now = datetime.now(timezone.utc)
        horizon = now + timedelta(days=days)
        matches: list[Match] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            starts_at = _parse_datetime(row.get("startsAt"))
            if starts_at is None or not (now - timedelta(hours=6) <= starts_at <= horizon):
                continue
            match = self._row_to_match(row, sport.lower())
            if match is not None:
                matches.append(match)
        matches.sort(key=lambda item: item.starts_at)
        logger.info("cito: {} → {} матчей на ближайшие {} дн.", sport, len(matches), days)
        return matches

    def _row_to_match(self, row: dict[str, Any], sport: str) -> Match | None:
        ext_id = row.get("id") or row.get("matchId")
        team1 = row.get("team1Name") or (row.get("team1") or {}).get("name")
        team2 = row.get("team2Name") or (row.get("team2") or {}).get("name")
        starts_at = _parse_datetime(row.get("startsAt"))
        if not ext_id or not team1 or not team2 or starts_at is None:
            return None
        event = row.get("event") if isinstance(row.get("event"), dict) else {}
        status = str(row.get("status") or "").lower()
        is_live = status == "live"
        return Match(
            ext_id=str(ext_id),
            sport=sport,
            league=str(row.get("eventName") or event.get("name") or "Unknown"),
            league_tier=tier_label(event.get("tier"), event.get("prestigeScore")),
            home_team=str(team1),
            away_team=str(team2),
            starts_at=starts_at,
            source=self.provider_name,
            status="live" if is_live else "scheduled",
            home_score=safe_int(row.get("team1Score")),
            away_score=safe_int(row.get("team2Score")),
            is_live=is_live,
            live_stage=str(row.get("stageName") or "") or None,
            extra={
                "cito_event_id": row.get("eventId"),
                "bestof": row.get("bestOf"),
                "prestige": event.get("prestigeScore"),
                "cito_status": status or None,
            },
        )

    # --------------------------------------------------------------------- лайв
    async def get_live_matches(self, sport: str) -> list[EsportsLiveMatch]:
        """Лайв-матчи Cito.

        ⚠️ По умолчанию выключено (CITO_LIVE_ENABLED=false): 45-секундный поллинг
        выест месячные 500 запросов за пару суток. Даже при включении ответ кэшируется
        (CITO_LIVE_TTL_SEC), а бюджет закрывает источник, когда лимит кончится.
        """
        prefix = self._sport_prefix(sport)
        if not prefix or not settings.cito_live_enabled:
            return []
        if prefix == "cs2":
            rows = await self._cached(
                "live:cs2", settings.cito_live_ttl_sec, "/cs2/live"
            )
        else:
            rows = await self._cached(
                "live:dota2", settings.cito_live_ttl_sec, "/dota2/matches/live"
            )
        if not isinstance(rows, list):
            return []
        live: list[EsportsLiveMatch] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            match = self._row_to_match(row, sport.lower())
            if match is None:
                continue
            live.append(
                EsportsLiveMatch(
                    ext_id=match.ext_id,
                    sport=sport.lower(),
                    league=match.league,
                    league_tier=match.league_tier,
                    team_a=match.home_team,
                    team_b=match.away_team,
                    source=self.provider_name,
                    match_time=match.starts_at,
                    series_score=(
                        (match.home_score, match.away_score)
                        if match.home_score is not None and match.away_score is not None
                        else None
                    ),
                    map_name=str(row.get("currentMap") or "") or None,
                    stage=str(row.get("stageName") or "лайв"),
                    extra={"cito": True, "bestof": row.get("bestOf")},
                )
            )
        return live

    async def get_draft(self, match_id: str) -> tuple[list[str], list[str], list[str], list[str]]:
        """Драфт Dota 2 (пики/баны) с /dota2/matches/{id}/draft.

        Основной источник драфта — OpenDota (в лайве). Здесь драфт запрашивается
        только если понадобится и ключ уже задан; структура строк разбирается терпимо.
        """
        data = await self._cached(
            f"draft:{match_id}", settings.cito_cache_ttl_sec, f"/dota2/matches/{match_id}/draft"
        )
        rows = data.get("rows") if isinstance(data, dict) else data
        if not isinstance(rows, list):
            return [], [], [], []
        picks_a: list[str] = []
        picks_b: list[str] = []
        bans_a: list[str] = []
        bans_b: list[str] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            hero = row.get("heroName") or row.get("hero") or row.get("hero_name")
            if not hero:
                continue
            side = str(row.get("side") or row.get("team") or "").lower()
            is_ban = bool(row.get("isBan") or row.get("is_ban")) or str(row.get("type") or "").lower() == "ban"
            team_a = side in ("radiant", "team1", "1", "a")
            target = bans_a if (is_ban and team_a) else bans_b if is_ban else picks_a if team_a else picks_b
            target.append(str(hero))
        return picks_a, picks_b, bans_a, bans_b

    # ------------------------------------------------------------ статистика карт
    async def get_map_stats(self, team: str, map_name: str) -> MapStats | None:
        """Винрейт команды на карте CS2 (`/cs2/teams/{slug}/stats` → `mapWinRates`).

        Для Dota 2 карта не имеет смысла (карта = матч) — возвращаем None.
        Один запрос на команду, кэш CITO_TEAM_TTL_SEC.
        """
        slug = team_slug(team)
        map_key = (map_name or "").strip().lower()
        if not slug or not map_key:
            return None
        data = await self._cached(f"team-stats:{slug}", settings.cito_team_ttl_sec, f"/cs2/teams/{slug}/stats")
        if not isinstance(data, dict):
            return None
        for entry in data.get("mapWinRates") or []:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("mapName") or entry.get("mapNameCanonical") or "").strip().lower()
            if not name or (map_key not in name and name not in map_key):
                continue
            played = safe_int(entry.get("played")) or 0
            win_rate = entry.get("winRate")
            return MapStats(
                team=team,
                map_name=str(entry.get("mapName") or map_name),
                matches=played,
                win_rate=round(float(win_rate), 4) if isinstance(win_rate, (int, float)) else None,
                round_win_rate=None,  # Cito не отдаёт раундовый винрейт в этом эндпоинте
                source="cito:mapWinRates",
            )
        logger.debug("cito: у '{}' нет статистики по карте '{}'", team, map_name)
        return None

    async def get_team_roster(self, sport: str, team: str) -> list[str] | None:
        """Состав команды (`/cs2/teams/{slug}/roster`). Для dota2 не поддерживается."""
        prefix = self._sport_prefix(sport)
        slug = team_slug(team)
        if prefix != "cs2" or not slug:
            return None
        rows = await self._cached(f"roster:{slug}", settings.cito_team_ttl_sec, f"/cs2/teams/{slug}/roster")
        if not isinstance(rows, list):
            return None
        players = [
            str(row.get("playerNickname") or row.get("playerName"))
            for row in rows
            if isinstance(row, dict) and (row.get("playerNickname") or row.get("playerName"))
        ]
        return players or None

    # -------------------------------------------------------------------- tier
    async def get_tournament_tier(self, tournament: str, sport: str = "cs2") -> str | None:
        """Tier турнира по каталогу событий Cito (CS2): «S»/«A»/... → «S-Tier»/«A-Tier».

        В Dota 2 у Cito нет каталога событий с tier — там используется OpenDota
        (`/leagues`), см. app/sources/opendota.py.
        """
        name = (tournament or "").strip()
        if not name or self._sport_prefix(sport) != "cs2":
            return None

        async def factory() -> dict[str, Any]:
            events: list[dict[str, Any]] = []
            for path, key in (("/cs2/events/upcoming", "events-upcoming"), ("/cs2/events/recent", "events-recent")):
                rows = await self._cached(key, settings.cito_catalog_ttl_sec, path, {"limit": 100})
                if isinstance(rows, list):
                    events.extend(row for row in rows if isinstance(row, dict))
            return {"events": events}

        catalog = await self._cache.get_or_set(
            "cs2:events", factory, ttl_sec=settings.cito_catalog_ttl_sec
        )
        events = catalog.get("events") if isinstance(catalog, dict) else None
        if not events:
            return None
        return self._best_event_tier(name, events)

    @staticmethod
    def _best_event_tier(name: str, events: Sequence[dict[str, Any]]) -> str | None:
        normalized = _normalize_name(name)
        if not normalized:
            return None
        best_score = 0.0
        best_event: dict[str, Any] | None = None
        for event in events:
            candidate = _normalize_name(str(event.get("name") or ""))
            if not candidate:
                continue
            score = 100.0 if candidate == normalized else float(
                max(fuzz.partial_ratio(normalized, candidate), fuzz.token_set_ratio(normalized, candidate))
            )
            if score > best_score:
                best_score, best_event = score, event
        if best_event is None or best_score < NAME_MATCH_THRESHOLD:
            if best_event is not None:
                logger.debug(
                    "cito: турнир '{}' не сопоставлен (лучший '{}', score={:.1f} < {})",
                    name, best_event.get("name"), best_score, NAME_MATCH_THRESHOLD,
                )
            return None
        return tier_label(best_event.get("tier"), best_event.get("prestigeScore"))
