"""API-Tennis (RapidAPI) — теннис: расписание, форма, H2H (Модуль 1).

⚠️ ЧЕСТНОЕ ПРЕДУПРЕЖДЕНИЕ (не удаляйте этот блок):
  Хост `api-tennis-api.p.rapidapi.com` и пути ниже — это КАНДИДАТЫ, а не документация.
  У RapidAPI-провайдеров тенниса пути и схемы ответов различаются между тарифами
  (`/tennis/v2/atp/fixtures/{date}`, `/tennis/v2/atp/player/{id}/matches` и т.п.).
  Поэтому провайдер:
    1) перебирает список кандидатов путей (TENNIS_PATH_CANDIDATES / PLAYER_PATH_CANDIDATES);
    2) запоминает тот путь, который реально ответил 200, и дальше использует только его;
    3) если ничего не сработало — пишет в лог инструкцию «открыть RapidAPI Playground,
       скопировать путь и вписать его в .env (см. SETUP.md, раздел «Теннис»)»
       и возвращает пустой результат. Сервис при этом продолжает работать:
       по теннису LLM-анализ возможен и без статистики (data_quality=weak → сигналов не будет,
       поэтому для продакшена путь ОБЯЗАТЕЛЬНО нужно подтвердить).

Как быстро проверить: scripts/check_sources.py --source tennis печатает сырой ответ
и подсказывает, какой из кандидатов сработал.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from loguru import logger

from app.config import settings
from app.sources.base import (
    BaseHttpClient,
    FormEntry,
    H2HRecord,
    Injury,
    Lineup,
    Match,
    ScheduleDensity,
    SourceError,
    StatsProvider,
    TTLCache,
)

# Кандидаты путей (проверьте в RapidAPI Playground вашего провайдера и оставьте рабочие).
FIXTURE_PATH_CANDIDATES: tuple[str, ...] = (
    "/tennis/v2/{tour}/fixtures/{day}",
    "/tennis/v2/{tour}/fixtures",
    "/fixtures",
    "/matches",
)
PLAYER_PATH_CANDIDATES: tuple[str, ...] = (
    "/tennis/v2/{tour}/player/{player_id}/matches",
    "/tennis/v2/{tour}/players/{player_id}/matches",
    "/players/{player_id}/matches",
)
H2H_PATH_CANDIDATES: tuple[str, ...] = (
    "/tennis/v2/{tour}/h2h/{first}/{second}",
    "/h2h/{first}/{second}",
)
TOURS = ("atp", "wta")
FORM_TTL_SEC = 6 * 3600
H2H_TTL_SEC = 24 * 3600
SEARCH_TTL_SEC = 24 * 3600


def _parse_datetime(raw: Any) -> datetime | None:
    if raw in (None, ""):
        return None
    if isinstance(raw, (int, float)):
        try:
            return datetime.fromtimestamp(float(raw), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(raw).strip()
    if text.isdigit() and len(text) >= 9:
        return _parse_datetime(int(text))
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _to_int(value: Any) -> int | None:
    try:
        if value in (None, "", "-"):
            return None
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _deep_rows(payload: Any, *name_fragments: str, depth: int = 6) -> list[dict[str, Any]]:
    """Все списки словарей, чьи ключи содержат любую из «подсказок» (устойчиво к схеме)."""
    results: list[dict[str, Any]] = []
    fragments = [fragment.lower() for fragment in name_fragments]

    def walk(node: Any, level: int) -> None:
        if level > depth:
            return
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(value, list) and all(isinstance(item, dict) for item in value) and value:
                    if not fragments or any(fragment in str(key).lower() for fragment in fragments):
                        results.extend(value)
                else:
                    walk(value, level + 1)
        elif isinstance(node, list):
            for item in node:
                walk(item, level + 1)

    walk(payload, 0)
    if not results and isinstance(payload, list):
        results = [item for item in payload if isinstance(item, dict)]
    return results


class ApiTennisProvider(StatsProvider):
    """Теннис: расписание/форма/H2H через RapidAPI-провайдера (пути — конфигурируемые кандидаты)."""

    provider_name = "api-tennis"
    sport = "tennis"

    def __init__(self, base_url: str | None = None, api_key: str | None = None) -> None:
        key = (api_key if api_key is not None else settings.rapidapi_key or "").strip()
        super().__init__(
            source_name="api-tennis",
            base_url=base_url or settings.api_tennis_base_url,
            headers={"X-RapidAPI-Key": key, "X-RapidAPI-Host": settings.rapidapi_host_apitennis},
        )
        self._key = key
        self._cache = TTLCache(ttl_sec=FORM_TTL_SEC)
        self._working_paths: dict[str, str] = {}

    @property
    def available(self) -> bool:
        if not self.base_url:
            return False
        if not self._key:
            logger.info("api-tennis: RAPIDAPI_KEY не задан — провайдер отключён")
            return False
        return True

    # ---------------------------------------------------------------- механика
    async def _try_paths(self, cache_key: str, candidates: tuple[str, ...], tour: str, **fmt: str) -> tuple[Any, str] | None:
        """Перебирает кандидаты путей; запоминает рабочий. Возвращает (payload, path) или None."""
        working = self._working_paths.get(cache_key)
        ordered = ((working,) if working else ()) + tuple(path for path in candidates if path != working)
        for path in ordered:
            url = path.format(tour=tour, **fmt)
            try:
                payload = await self.get_json(url)
            except SourceError as exc:
                logger.debug("api-tennis: {} → {} ({})", url, "не 200", str(exc)[:140])
                continue
            except Exception as exc:
                logger.debug("api-tennis: {} → сеть/таймаут ({})", url, exc)
                continue
            if payload:
                self._working_paths[cache_key] = path
                logger.info("api-tennis: рабочий путь для {} = {}", cache_key, path)
                return payload, path
        logger.warning(
            "api-tennis: ни один из путей {} не ответил для tour={}. "
            "Откройте RapidAPI Playground вашего теннисного провайдера, скопируйте рабочий путь "
            "и добавьте его в FIXTURE_PATH_CANDIDATES/PLAYER_PATH_CANDIDATES "
            "(app/sources/apitennis.py), либо задайте прокси-хост в API_TENNIS_BASE_URL.",
            candidates, tour,
        )
        return None

    # ------------------------------------------------------------- расписание
    async def get_upcoming(self, day: date | None = None) -> list[Match]:
        """Матчи тенниса на дату (прематч)."""
        target = (day or datetime.now(timezone.utc).date()).isoformat()
        matches: list[Match] = []
        for tour in TOURS:
            result = await self._try_paths(f"fixtures:{tour}", FIXTURE_PATH_CANDIDATES, tour, day=target)
            if not result:
                continue
            payload, _ = result
            for row in _deep_rows(payload, "fixture", "match", "data", "result", "response", "event"):
                home = self._player_name(row, ("player1", "home", "first", "p1"))
                away = self._player_name(row, ("player2", "away", "second", "p2"))
                if not home or not away:
                    continue
                moment = _parse_datetime(
                    row.get("date") or row.get("startTime") or row.get("time") or row.get("matchTime")
                ) or datetime.combine(day or datetime.now(timezone.utc).date(), datetime.min.time(), tzinfo=timezone.utc)
                league = self._league_name(row) or ("ATP" if tour == "atp" else "WTA")
                matches.append(
                    Match(
                        ext_id=f"tennis:{tour}:{row.get('id') or f'{home}-{away}-{moment.isoformat()}'}",
                        sport="tennis",
                        league=league,
                        home_team=home,
                        away_team=away,
                        starts_at=moment,
                        source="api-tennis",
                        status="finished" if self._is_finished(row) else "scheduled",
                        home_score=_to_int(self._score(row, "player1", "home", "first")),
                        away_score=_to_int(self._score(row, "player2", "away", "second")),
                        extra={"tour": tour, "round": row.get("round") or row.get("stage")},
                    )
                )
        return matches

    @staticmethod
    def _player_name(row: dict[str, Any], keys: tuple[str, ...]) -> str | None:
        for key in keys:
            value = row.get(key)
            if isinstance(value, dict):
                name = value.get("name") or value.get("fullName") or value.get("player")
                if name:
                    return str(name)
            elif isinstance(value, str) and value:
                return value
        nested = row.get("players")
        if isinstance(nested, list):
            for key in keys:
                for player in nested:
                    if isinstance(player, dict) and str(player.get("position") or player.get("id")) == key:
                        name = player.get("name") or player.get("fullName")
                        if name:
                            return str(name)
        return None

    @staticmethod
    def _score(row: dict[str, Any], *keys: str) -> Any:
        for key in keys:
            candidate = row.get(f"{key}Score") or row.get(f"score{key.capitalize()}")
            if candidate is not None:
                return candidate
        scores = row.get("scores") or row.get("score")
        if isinstance(scores, dict):
            for key in keys:
                if key in scores:
                    return scores[key]
        return None

    @staticmethod
    def _league_name(row: dict[str, Any]) -> str | None:
        for key in ("tournament", "league", "competition", "series"):
            value = row.get(key)
            if isinstance(value, dict):
                name = value.get("name") or value.get("title")
                if name:
                    return str(name)
            elif isinstance(value, str) and value:
                return value
        return None

    @staticmethod
    def _is_finished(row: dict[str, Any]) -> bool:
        status = str(row.get("status") or row.get("matchStatus") or "").lower()
        return status in ("finished", "final", "ended", "retired", "walkover") or bool(row.get("winner"))

    # --------------------------------------------------------- форма и H2H
    async def _find_player_id(self, name: str, tour: str) -> str | None:
        async def factory() -> str | None:
            payload, _ = await self._try_paths(
                f"players:{tour}", ("/tennis/v2/{tour}/players", "/players", "/tennis/v2/{tour}/player"), tour
            ) or (None, "")
            if not payload:
                return None
            target = name.strip().lower()
            for row in _deep_rows(payload, "player", "data", "result", "response"):
                candidate = str(row.get("name") or row.get("fullName") or "").strip()
                if candidate and (candidate.lower() == target or target in candidate.lower() or candidate.lower() in target):
                    player_id = row.get("id") or row.get("playerId") or row.get("key")
                    return str(player_id) if player_id else None
            return None

        return await self._cache.get_or_set(f"playerid:{tour}:{name.lower()}", factory, ttl_sec=SEARCH_TTL_SEC)

    async def get_recent_form(self, team: str, n: int = 5, **kwargs: Any) -> list[FormEntry]:
        """Последние матчи игрока (team = имя игрока)."""
        tour = kwargs.get("tour") or ("wta" if kwargs.get("is_wta") else "atp")
        player_id = await self._find_player_id(team, tour)
        if not player_id:
            logger.info("api-tennis: не найден id игрока '{}' (проверьте путь /players)", team)
            return []

        async def factory() -> list[FormEntry]:
            result = await self._try_paths(f"player:{tour}", PLAYER_PATH_CANDIDATES, tour, player_id=player_id)
            if not result:
                return []
            payload, _ = result
            entries: list[tuple[datetime, FormEntry]] = []
            for row in _deep_rows(payload, "match", "fixture", "data", "result", "response"):
                moment = _parse_datetime(row.get("date") or row.get("startTime") or row.get("time"))
                home = self._player_name(row, ("player1", "home", "first", "p1"))
                away = self._player_name(row, ("player2", "away", "second", "p2"))
                if not home or not away or not self._is_finished(row):
                    continue
                first_score = _to_int(self._score(row, "player1", "home", "first"))
                second_score = _to_int(self._score(row, "player2", "away", "second"))
                is_home = team.strip().lower() in home.strip().lower()
                scored, missed = (first_score, second_score) if is_home else (second_score, first_score)
                result_marker = "W" if (scored is not None and missed is not None and scored > missed) else (
                    "L" if scored is not None and missed is not None else None
                )
                entry = FormEntry(
                    date=moment,
                    opponent=away if is_home else home,
                    is_home=is_home,
                    goals_for=scored,
                    goals_against=missed,
                    result=result_marker,
                    competition=self._league_name(row) or tour.upper(),
                )
                entries.append((moment or datetime.min.replace(tzinfo=timezone.utc), entry))
            entries.sort(key=lambda item: item[0], reverse=True)
            return [entry for _, entry in entries[:n]]

        return await self._cache.get_or_set(f"form:{tour}:{player_id}", factory, ttl_sec=FORM_TTL_SEC)

    async def get_h2h(self, home_team: str, away_team: str, **kwargs: Any) -> list[H2HRecord]:
        """Личные встречи двух игроков."""
        tour = kwargs.get("tour") or "atp"
        first = await self._find_player_id(home_team, tour)
        second = await self._find_player_id(away_team, tour)
        if not first or not second:
            return []

        async def factory() -> list[H2HRecord]:
            result = await self._try_paths(f"h2h:{tour}", H2H_PATH_CANDIDATES, tour, first=first, second=second)
            if not result:
                return []
            payload, _ = result
            records: list[H2HRecord] = []
            for row in _deep_rows(payload, "match", "h2h", "data", "result", "response"):
                records.append(
                    H2HRecord(
                        date=_parse_datetime(row.get("date") or row.get("startTime")),
                        home_team=self._player_name(row, ("player1", "home", "first", "p1")) or home_team,
                        away_team=self._player_name(row, ("player2", "away", "second", "p2")) or away_team,
                        home_score=_to_int(self._score(row, "player1", "home", "first")),
                        away_score=_to_int(self._score(row, "player2", "away", "second")),
                        competition=self._league_name(row) or tour.upper(),
                    )
                )
            records.sort(key=lambda record: record.date or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
            return records[: settings.h2h_limit]

        return await self._cache.get_or_set(f"h2h:{tour}:{first}:{second}", factory, ttl_sec=H2H_TTL_SEC)

    # ------------------------------------------------------ остальные поля Stats
    async def get_injuries(self, team: str, **kwargs: Any) -> list[Injury]:
        """Теннисный провайдер травмы не отдаёт: снятия/травмы приходят из новостей (LLM)."""
        return []

    async def get_predicted_lineups(self, team: str, **kwargs: Any) -> Lineup | None:
        return None

    async def get_schedule_density(self, team: str, **kwargs: Any) -> ScheduleDensity:
        """У теннисиста «плотность» = сколько матчей за 7 дней (турнирные сетки)."""
        form = await self.get_recent_form(team, n=7, **kwargs)
        now = datetime.now(timezone.utc)
        last_week = now - timedelta(days=7)
        played = [entry for entry in form if entry.date and last_week <= entry.date <= now]
        last_match = max((entry.date for entry in played), default=None)
        return ScheduleDensity(
            matches_last_7_days=len(played),
            matches_next_7_days=0,
            rest_days=(now - last_match).days if last_match else None,
            back_to_back=len(played) >= 4,  # в теннисе это «играл почти каждый день»
            notes="api-tennis: по последним матчам игрока",
        )
