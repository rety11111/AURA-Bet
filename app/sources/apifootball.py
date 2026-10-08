"""API-Football (RapidAPI) — футбольная статистика: травмы, составы, форма, H2H.

Особенности free-тарифа (важно!): у API-Football бесплатный план — 100 запросов
в сутки и только сезон {текущий-2}..{текущий}:  ограничение по сезону у разных
планов различается, поэтому season берётся из конфига/параметра. Все ответы
кэшируются в памяти (TTL из config) и в таблице xg_cache (для xG — см. understat.py),
чтобы не сжигать лимит.

REST-схема API v3: https://www.api-football.com/documentation-v3
  GET /fixtures?team={id}&last=5
  GET /fixtures?headtohead={id1}-{id2}&last=10
  GET /injuries?team={id}&season={season}
  GET /fixtures/lineups?fixture={id}      (подтверждённые составы)
  GET /fixtures?team={id}&next=1
  GET /teams?search={name}

Если какой-то эндпоинт у вашего плана недоступен, метод вернёт пустой список и
залогирует причину — сервис продолжит работу (data_quality может стать «weak»).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from loguru import logger

from app.config import settings
from app.sources.base import (
    FormEntry,
    H2HRecord,
    Injury,
    Lineup,
    ScheduleDensity,
    SourceError,
    StatsProvider,
    TTLCache,
    safe_int,
)

_TEAM_CACHE_TTL = 24 * 3600


class ApiFootballProvider(StatsProvider):
    provider_name = "apifootball"
    sport = "football"

    def __init__(self, api_key: str | None = None, host: str | None = None, season: int | None = None, **kwargs: Any) -> None:
        super().__init__(source_name=self.provider_name, **kwargs)
        self.api_key = api_key if api_key is not None else settings.rapidapi_key
        self.host = host or settings.rapidapi_host_apifootball
        self.base_url = f"https://{self.host}"
        self.season = season or datetime.now(timezone.utc).year
        self._cache = TTLCache(ttl_sec=settings.cache_ttl_form_sec, maxsize=2048)
        self._team_cache = TTLCache(ttl_sec=_TEAM_CACHE_TTL, maxsize=4096)

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "X-RapidAPI-Key": self.api_key,
            "X-RapidAPI-Host": self.host,
        }

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        if not self.available:
            raise SourceError(f"{self.provider_name}: RAPIDAPI_KEY не задан (см. SETUP.md)")
        payload = await self.get_json(f"{self.base_url}{path}", params=params or {}, headers=self._headers)
        if isinstance(payload, dict):
            errors = payload.get("errors")
            if errors and (isinstance(errors, dict) and any(errors.values())):
                logger.warning("{}: API вернул ошибки для {}: {}", self.provider_name, path, errors)
                return None
            return payload.get("response")
        return None

    # -------------------------------------------------------------- team ids
    async def resolve_team_id(self, team_name: str) -> int | None:
        key = team_name.strip().lower()

        async def factory() -> int | None:
            response = await self._get("/teams", {"search": team_name})
            if not response:
                return None
            candidates: list[tuple[float, int]] = []
            from rapidfuzz import fuzz

            for item in response:
                team = item.get("team") if isinstance(item, dict) else None
                if not isinstance(team, dict):
                    continue
                api_name = str(team.get("name") or "")
                score = fuzz.token_set_ratio(key, api_name.lower())
                team_id = safe_int(team.get("id"))
                if team_id:
                    candidates.append((score, team_id))
            if not candidates:
                return None
            best_score, best_id = max(candidates)
            if best_score < 80:
                logger.warning(
                    "{}: имя «{}» не сопоставлено с API-Football (лучший вариант {}, score={:.1f})",
                    self.provider_name, team_name, best_id, best_score,
                )
                return None
            return best_id

        return await self._team_cache.get_or_set(key, factory)

    # --------------------------------------------------------------- injuries
    async def get_injuries(self, team: str, **kwargs: Any) -> list[Injury]:
        team_id = kwargs.get("team_id") or await self.resolve_team_id(team)
        if not team_id:
            return []
        cache_key = f"injuries:{team_id}:{self.season}"

        async def factory() -> list[Injury]:
            response = await self._get("/injuries", {"team": team_id, "season": self.season})
            out: list[Injury] = []
            for item in response or []:
                player = (item.get("player") or {}) if isinstance(item, dict) else {}
                out.append(
                    Injury(
                        team=team,
                        player=str(player.get("name") or "unknown"),
                        status="out" if str(player.get("type", "")).lower() in ("missing fixture", "out") else "doubtful",
                        reason=str(player.get("reason") or "") or None,
                        source=self.provider_name,
                    )
                )
            logger.debug("{}: травмы {} → {}", self.provider_name, team, len(out))
            return out

        return await self._cache.get_or_set(cache_key, factory, ttl_sec=settings.cache_ttl_injuries_sec)

    # --------------------------------------------------------------- lineups
    async def get_predicted_lineups(self, team: str, **kwargs: Any) -> Lineup | None:
        fixture_id = kwargs.get("fixture_id")
        if not fixture_id:
            return None
        cache_key = f"lineup:{fixture_id}:{team}"

        async def factory() -> Lineup | None:
            response = await self._get("/fixtures/lineups", {"fixture": fixture_id})
            for item in response or []:
                side = item.get("team") if isinstance(item, dict) else None
                if not isinstance(side, dict):
                    continue
                if team.strip().lower() not in str(side.get("name", "")).lower() and len(response or []) > 1:
                    continue
                players = [str((p.get("player") or {}).get("name")) for p in item.get("startXI") or []]
                goalkeeper = str(item.get("goalkeeper")) if item.get("goalkeeper") else None
                return Lineup(
                    team=str(side.get("name") or team),
                    players=[p for p in players if p and p != "None"],
                    confirmed=True,  # /fixtures/lineups отдаёт уже подтверждённый состав
                    goalkeeper=goalkeeper,
                    formation=str(item.get("formation")) if item.get("formation") else None,
                    source=self.provider_name,
                )
            return None

        return await self._cache.get_or_set(cache_key, factory, ttl_sec=settings.cache_ttl_lineups_sec)

    # ------------------------------------------------------------------ form
    async def get_recent_form(self, team: str, n: int = 5, **kwargs: Any) -> list[FormEntry]:
        team_id = kwargs.get("team_id") or await self.resolve_team_id(team)
        if not team_id:
            return []
        cache_key = f"form:{team_id}:{n}"

        async def factory() -> list[FormEntry]:
            response = await self._get("/fixtures", {"team": team_id, "last": n})
            out: list[FormEntry] = []
            for item in response or []:
                if not isinstance(item, dict):
                    continue
                fixture = item.get("fixture") or {}
                teams = item.get("teams") or {}
                goals = item.get("goals") or {}
                is_home = safe_int((teams.get("home") or {}).get("id")) == team_id
                gf = safe_int(goals.get("home" if is_home else "away"))
                ga = safe_int(goals.get("away" if is_home else "home"))
                result = None
                if gf is not None and ga is not None:
                    result = "W" if gf > ga else ("D" if gf == ga else "L")
                out.append(
                    FormEntry(
                        date=_parse_dt(fixture.get("date")),
                        opponent=str(((teams.get("away") if is_home else teams.get("home")) or {}).get("name") or ""),
                        is_home=is_home,
                        goals_for=gf,
                        goals_against=ga,
                        result=result,
                        competition=str((item.get("league") or {}).get("name") or "") or None,
                    )
                )
            return out

        return await self._cache.get_or_set(cache_key, factory, ttl_sec=settings.cache_ttl_form_sec)

    # ------------------------------------------------------------------ h2h
    async def get_h2h(self, home_team: str, away_team: str, **kwargs: Any) -> list[H2HRecord]:
        home_id = kwargs.get("home_team_id") or await self.resolve_team_id(home_team)
        away_id = kwargs.get("away_team_id") or await self.resolve_team_id(away_team)
        if not home_id or not away_id:
            return []
        cache_key = f"h2h:{home_id}:{away_id}"

        async def factory() -> list[H2HRecord]:
            response = await self._get("/fixtures/headtohead", {"h2h": f"{home_id}-{away_id}", "last": 10})
            out: list[H2HRecord] = []
            for item in response or []:
                if not isinstance(item, dict):
                    continue
                teams = item.get("teams") or {}
                goals = item.get("goals") or {}
                out.append(
                    H2HRecord(
                        date=_parse_dt((item.get("fixture") or {}).get("date")),
                        home_team=str((teams.get("home") or {}).get("name") or ""),
                        away_team=str((teams.get("away") or {}).get("name") or ""),
                        home_score=safe_int(goals.get("home")),
                        away_score=safe_int(goals.get("away")),
                        competition=str((item.get("league") or {}).get("name") or "") or None,
                    )
                )
            return out

        return await self._cache.get_or_set(cache_key, factory, ttl_sec=settings.cache_ttl_h2h_sec)

    # ------------------------------------------------------- density/schedule
    async def get_schedule_density(self, team: str, **kwargs: Any) -> ScheduleDensity:
        team_id = kwargs.get("team_id") or await self.resolve_team_id(team)
        if not team_id:
            return ScheduleDensity()
        cache_key = f"density:{team_id}"

        async def factory() -> ScheduleDensity:
            schedule = await self._get("/fixtures", {"team": team_id, "next": 3})
            now = datetime.now(timezone.utc)
            next_dates = [
                _parse_dt((item.get("fixture") or {}).get("date"))
                for item in schedule or []
                if isinstance(item, dict)
            ]
            next_dates = [d for d in next_dates if d is not None]
            recent = await self.get_recent_form(team, n=6, team_id=team_id)
            recent_dates = [entry.date for entry in recent if entry.date]
            last_7 = sum(1 for d in recent_dates if d and (now - d) <= timedelta(days=7))
            next_7 = sum(1 for d in next_dates if d and (d - now) <= timedelta(days=7))
            rest_days = None
            if recent_dates:
                most_recent = max(recent_dates)
                rest_days = max(0, (now - most_recent).days)
            return ScheduleDensity(
                matches_last_7_days=last_7,
                matches_next_7_days=next_7,
                rest_days=rest_days,
                back_to_back=bool(rest_days is not None and rest_days <= 1),
                notes="источник: API-Football fixtures",
            )

        return await self._cache.get_or_set(cache_key, factory, ttl_sec=settings.cache_ttl_form_sec)


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None
