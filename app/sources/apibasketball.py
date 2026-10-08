"""Баскетбол: API-Basketball (RapidAPI) + BallDontLie как фолбэк для NBA.

Источники:
  1) API-Basketball — https://api-basketball.p.rapidapi.com (RapidAPI).
     Хост и заголовок X-RapidAPI-Key — как в RapidAPI Playground. ВАЖНО: на free-плане
     доступны /games, /teams, /leagues; эндпоинт /games/statistics может требовать
     платный тариф. Мы это учитываем: если статистик нет, считаем ORtg/DRtg
     по очкам относительно среднего лиги (оценка помечается в notes/extra).
  2) BallDontLie — https://api.balldontlie.io/v1 (NBA, бесплатный tier: 5 запросов/мин
     без ключа для v1; v2 требует ключ в заголовке Authorization). Используется как
     фолбэк, когда API-Basketball молчит, и как источник счётов по играм.

Обе части возвращают «игру» и агрегированную статистику команды (BasketballTeamStats)
для Модуля 3 (модель pace/ORtg/DRtg).

Честно про оценки:
  * possessions ≈ FGA + 0.44×FTA − OREB + TOV — классическая формула Оливера.
    Если API не отдаёт FGA/FTA/OREB/TOV (free plan), мы НЕ выдумываем значения:
    помечаем data_quality=weak и считаем эффективность по очкам за игру
    относительно среднего лиги (это честная грубая оценка, а не «настоящий» ORtg).
  * Разбивки home/away в агрегатах API-Basketball нет → домашнее преимущество
    (2.5 очка) применяется в модели stats_models/basketball.py, как и для NHL.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from loguru import logger

from app.config import settings
from app.sources.base import (
    BasketballTeamStats,
    BaseHttpClient,
    FormEntry,
    Match,
    ScheduleDensity,
    SourceError,
    StatsProvider,
    TTLCache,
)

# Средняя результативность лиги (очки на команду за игру) — для нормализации оценки ORtg.
LEAGUE_AVG_PPG = {"nba": 114.0, "euroleague": 80.0, "default": 80.0}
STATS_TTL_SEC = 6 * 3600
GAMES_WINDOW_DAYS = 30


def _epoch_to_datetime(value: Any) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def _parse_datetime(raw: Any) -> datetime | None:
    if raw in (None, ""):
        return None
    if isinstance(raw, (int, float)):
        return _epoch_to_datetime(raw)
    text = str(raw).strip()
    if text.isdigit():
        return _epoch_to_datetime(int(text))
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(text, fmt)
            return parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _to_float(value: Any) -> float | None:
    try:
        if value in (None, "", "-"):
            return None
        return float(str(value).replace("%", ""))
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    number = _to_float(value)
    return int(number) if number is not None else None


def _stat_map(stat_rows: list[dict[str, Any]]) -> dict[str, float]:
    """[{'type': 'Points', 'value': 112}, ...] → {'points': 112.0, ...} (нормализованные ключи)."""
    mapping: dict[str, float] = {}
    for row in stat_rows or []:
        if not isinstance(row, dict):
            continue
        key = str(row.get("type") or row.get("name") or "").strip().lower()
        value = _to_float(row.get("value") if "value" in row else row.get("stat"))
        if key and value is not None:
            mapping[key] = value
    return mapping


def _pick_stat(stats: dict[str, float], *fragments: str) -> float | None:
    for fragment in fragments:
        for key, value in stats.items():
            if fragment in key:
                return value
    return None


def possessions_from_boxscore(stats: dict[str, float]) -> float | None:
    """Оценка владений по формуле Оливера: FGA + 0.44×FTA − OREB + TOV."""
    fga = _pick_stat(stats, "field goal", "field_goals", "shot")
    fta = _pick_stat(stats, "free throw", "free_throw")
    oreb = _pick_stat(stats, "offensive rebound", "offensive_rebound")
    turnovers = _pick_stat(stats, "turnover")
    if fga is None or fta is None or oreb is None or turnovers is None:
        return None
    return fga + 0.44 * fta - oreb + turnovers


def efficiency_from_stats(stats: dict[str, float], stats_against: dict[str, float]) -> tuple[float, float] | None:
    """(ORtg, DRtg) на 100 владений, если данных достаточно; иначе None."""
    own = possessions_from_boxscore(stats)
    opp = possessions_from_boxscore(stats_against)
    points_for = _pick_stat(stats, "points")
    points_against = _pick_stat(stats_against, "points")
    if not own or not opp or points_for is None or points_against is None:
        return None
    return round(100.0 * points_for / own, 2), round(100.0 * points_against / opp, 2)


class ApiBasketballProvider(StatsProvider):
    """API-Basketball (RapidAPI) — расписания, счёты, статистика баскетбола."""

    provider_name = "api-basketball"
    sport = "basketball"

    def __init__(self, base_url: str | None = None, api_key: str | None = None) -> None:
        key = api_key if api_key is not None else settings.rapidapi_key
        headers = {
            "X-RapidAPI-Key": key or "",
            "X-RapidAPI-Host": settings.rapidapi_host_apibasketball,
        }
        super().__init__(source_name="api-basketball", base_url=base_url or settings.api_basketball_base_url, headers=headers)
        self._cache = TTLCache(ttl_sec=STATS_TTL_SEC)
        self._stats_supported: bool | None = None  # становится False после первой ошибки /games/statistics

    @property
    def available(self) -> bool:
        if not self.base_url:
            return False
        if not self.headers.get("X-RapidAPI-Key"):
            logger.info("api-basketball: RAPIDAPI_KEY не задан — провайдер отключён")
            return False
        return True

    # ------------------------------------------------------------------ запросы
    async def _request(self, path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        payload = await self.get_json(path, params=params)
        if isinstance(payload, dict) and payload.get("errors"):
            errors = payload["errors"]
            if errors:
                raise SourceError(f"api-basketball: {errors}")
        rows = payload.get("response", []) if isinstance(payload, dict) else []
        if not isinstance(rows, list):
            raise SourceError("api-basketball: неожиданная схема ответа (нужен список response)")
        return [row for row in rows if isinstance(row, dict)]

    async def get_games(self, day: date, league_id: int | None = None, season: str | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"date": day.isoformat()}
        if league_id:
            params["league"] = str(league_id)
        if season:
            params["season"] = season
        return await self._request("/games", params)

    async def get_games_for_team(self, team_id: int, season: str | None = None, last: int = 10) -> list[dict[str, Any]]:
        """Последние завершённые игры команды (для формы и средних)."""
        params: dict[str, Any] = {"team": str(team_id)}
        if season:
            params["season"] = season
        rows = await self._request("/games", params)
        finished = [
            row
            for row in rows
            if str(((row.get("status") or {}).get("short")) or "").upper() in ("FT", "AOT", "AP", "AET")
        ]
        finished.sort(key=lambda row: _parse_datetime(row.get("date")) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        return finished[:last]

    async def _game_statistics(self, game_id: int) -> list[dict[str, Any]] | None:
        """Статистика игры; на free-плане может быть недоступна → None (деградация)."""
        if self._stats_supported is False:
            return None
        try:
            rows = await self._request("/games/statistics", {"id": str(game_id)})
            self._stats_supported = True
            return rows
        except SourceError as exc:
            self._stats_supported = False
            logger.info(
                "api-basketball: /games/statistics недоступен на текущем тарифе ({}) — "
                "ORtg/DRtg будут оценены по очкам (data_quality=weak)",
                str(exc)[:160],
            )
            return None

    async def search_team(self, name: str) -> dict[str, Any] | None:
        """Команда по названию (id нужен для /games?team=)."""
        rows = await self._request("/teams", {"search": name})
        if not rows:
            return None
        exact = [row for row in rows if str(row.get("name") or "").strip().lower() == name.strip().lower()]
        return exact[0] if exact else rows[0]

    # ----------------------------------------------------------- интерфейс Stats
    async def get_recent_form(self, team: str, n: int = 5, **kwargs: Any) -> list[FormEntry]:
        team_id = (await self.search_team(team) or {}).get("id")
        if not team_id:
            return []
        entries: list[FormEntry] = []
        for game in await self.get_games_for_team(int(team_id), kwargs.get("season"), last=n):
            home = (game.get("teams") or {}).get("home") or {}
            away = (game.get("teams") or {}).get("away") or {}
            scores = game.get("scores") or {}
            home_points = _to_float(((scores.get("home") or {}).get("total")))
            away_points = _to_float(((scores.get("away") or {}).get("total")))
            if home_points is None or away_points is None:
                continue
            is_home = str(home.get("id")) == str(team_id)
            scored = home_points if is_home else away_points
            missed = away_points if is_home else home_points
            opponent = away if is_home else home
            entries.append(
                FormEntry(
                    date=_parse_datetime(game.get("date")),
                    opponent=str(opponent.get("name") or ""),
                    is_home=is_home,
                    goals_for=int(scored),
                    goals_against=int(missed),
                    result="W" if scored > missed else "L",
                    competition=str((game.get("league") or {}).get("name") or "basketball"),
                )
            )
        return entries

    async def get_h2h(self, home_team: str, away_team: str, **kwargs: Any) -> list[Any]:
        """H2H по играм первой команды (API-Basketball не имеет отдельного H2H на free-плане)."""
        from app.sources.base import H2HRecord

        team_id = (await self.search_team(home_team) or {}).get("id")
        if not team_id:
            return []
        opponent_key = away_team.strip().lower()
        records: list[H2HRecord] = []
        for game in await self.get_games_for_team(int(team_id), kwargs.get("season"), last=30):
            home = (game.get("teams") or {}).get("home") or {}
            away = (game.get("teams") or {}).get("away") or {}
            names = {str(home.get("name") or "").lower(), str(away.get("name") or "").lower()}
            if opponent_key not in names:
                continue
            scores = game.get("scores") or {}
            records.append(
                H2HRecord(
                    date=_parse_datetime(game.get("date")),
                    home_team=str(home.get("name") or ""),
                    away_team=str(away.get("name") or ""),
                    home_score=_int_or_none((scores.get("home") or {}).get("total")),
                    away_score=_int_or_none((scores.get("away") or {}).get("total")),
                    competition=str((game.get("league") or {}).get("name") or ""),
                )
            )
        return records[: settings.h2h_limit]

    async def get_injuries(self, team: str, **kwargs: Any) -> list[Any]:
        """API-Basketball травмы не отдаёт — контекст травм по баскетболу даёт LLM/новости."""
        return []

    async def get_predicted_lineups(self, team: str, **kwargs: Any) -> Any:
        return None

    async def get_schedule_density(self, team: str, **kwargs: Any) -> ScheduleDensity:
        team_id = (await self.search_team(team) or {}).get("id")
        if not team_id:
            return ScheduleDensity(notes="api-basketball: команда не найдена")
        now = datetime.now(timezone.utc)
        games = await self.get_games_for_team(int(team_id), kwargs.get("season"), last=20)
        last_week = now - timedelta(days=7)
        played_last = [game for game in games if (moment := _parse_datetime(game.get("date"))) and last_week <= moment <= now]
        last_game = max((moment for game in played_last if (moment := _parse_datetime(game.get("date")))), default=None)
        return ScheduleDensity(
            matches_last_7_days=len(played_last),
            matches_next_7_days=0,  # расписание на будущее требует отдельного запроса по датам
            rest_days=(now - last_game).days if last_game else None,
            back_to_back=bool(last_game and (now - last_game) < timedelta(hours=30)),
            three_in_four=len(played_last) >= 3,
            notes="api-basketball: по последним играм",
        )

    # ----------------------------------------------------------- модель: pace/ORtg
    async def get_team_stats(self, team: str, season: str | None = None, last: int = 10) -> BasketballTeamStats | None:
        """BasketballTeamStats: pace/ORtg/DRtg по последним играм (или честная оценка)."""
        cache_key = f"team:{team.lower()}:{season}:{last}"
        return await self._cache.get_or_set(cache_key, lambda: self._build_team_stats(team, season, last))

    async def _build_team_stats(self, team: str, season: str | None, last: int) -> BasketballTeamStats | None:
        team_row = await self.search_team(team)
        if not team_row:
            logger.debug("api-basketball: команда '{}' не найдена", team)
            return None
        team_id = int(team_row["id"])
        games = await self.get_games_for_team(team_id, season, last=last)
        if not games:
            return None

        points_for = points_against = 0.0
        ortg_values: list[float] = []
        drtg_values: list[float] = []
        pace_values: list[float] = []
        count = 0
        for game in games:
            scores = game.get("scores") or {}
            home_points = _to_float((scores.get("home") or {}).get("total"))
            away_points = _to_float((scores.get("away") or {}).get("total"))
            if home_points is None or away_points is None:
                continue
            is_home = str(((game.get("teams") or {}).get("home") or {}).get("id")) == str(team_id)
            scored, missed = (home_points, away_points) if is_home else (away_points, home_points)
            points_for += scored
            points_against += missed
            count += 1

            stats_rows = (await self._game_statistics(int(game["id"]))) if game.get("id") else None
            if not stats_rows:
                continue
            stats_by_team: dict[str, dict[str, float]] = {}
            for row in stats_rows:
                row_team_id = str(((row.get("team") or {})).get("id"))
                stats_by_team[row_team_id] = _stat_map(row.get("statistics") or [])
            own = stats_by_team.get(str(team_id))
            opponent_id = next((key for key in stats_by_team if key != str(team_id)), None)
            opponent = stats_by_team.get(opponent_id) if opponent_id else None
            if not own or not opponent:
                continue
            efficiency = efficiency_from_stats(own, opponent)
            if not efficiency:
                continue
            ortg, drtg = efficiency
            possessions = possessions_from_boxscore(own)
            ortg_values.append(ortg)
            drtg_values.append(drtg)
            if possessions:
                # pace = владений за игру (NBA считает на 48 минут, FIBA/Евролига — на 40;
                # для Евролиги множитель 48/40 применяется в конфиге через league_key).
                pace_multiplier = 1.0 if "nba" in league_key else 48.0 / 40.0
                pace_values.append(possessions * pace_multiplier)

        if count == 0:
            return None

        ppg_for = points_for / count
        ppg_against = points_against / count
        league_key = (season or "").lower()
        league_avg = LEAGUE_AVG_PPG.get("nba" if "nba" in league_key else "default", LEAGUE_AVG_PPG["default"])

        base_ortg = 114.0 if "nba" in league_key else 108.0
        if ortg_values and drtg_values:
            ortg = round(sum(ortg_values) / len(ortg_values), 2)
            drtg = round(sum(drtg_values) / len(drtg_values), 2)
            pace = round(sum(pace_values) / len(pace_values), 2) if pace_values else 100.0
            notes = "ORtg/DRtg/pace из boxscore (/games/statistics)"
            weak = False
        else:
            # Честная грубая оценка: нормализуем средний ORtg лиги по очкам команды.
            ortg = round(base_ortg * (ppg_for / league_avg), 2)
            drtg = round(base_ortg * (ppg_against / league_avg), 2)
            pace = settings.basketball_possessions_per_48
            notes = "оценка ORtg/DRtg по очкам (boxscore недоступен на плане RapidAPI) → data_quality=weak"
            weak = True

        logger.info(
            "api-basketball: {} — {} игр, ORtg={} DRtg={} pace={} ({})",
            team, count, ortg, drtg, pace, "boxscore" if not weak else "оценка по очкам",
        )
        return BasketballTeamStats(
            team=team,
            pace=pace,
            ortg=ortg,
            drtg=drtg,
            games=count,
            source="api-basketball" + ("" if not weak else ":estimated"),
        )


class BallDontLieProvider(StatsProvider):
    """BallDontLie — бесплатный источник счётов NBA (фолбэк для API-Basketball).

    ВАЖНО: v1 (api.balldontlie.io/v1) отдаёт игры без ключа, но с лимитом 5 запросов/мин;
    v2 требует ключ (Authorization: <key>). Если задан BALLDONTLIE_API_KEY — используем v2,
    иначе v1. Точный тариф проверяйте на https://www.balldontlie.io (scripts/check_sources.py).
    """

    provider_name = "balldontlie"
    sport = "basketball"

    def __init__(self, base_url: str | None = None, api_key: str | None = None) -> None:
        key = (api_key if api_key is not None else settings.balldontlie_api_key or "").strip()
        version = "v2" if key else "v1"
        default_base = f"https://api.balldontlie.io/{version}"
        headers = {"Accept": "application/json"}
        if key:
            headers["Authorization"] = key
        super().__init__(source_name="balldontlie", base_url=base_url or settings.balldontlie_base or default_base, headers=headers)
        self._cache = TTLCache(ttl_sec=STATS_TTL_SEC)
        self._teams: dict[str, dict[str, Any]] = {}

    @property
    def available(self) -> bool:
        return bool(self.base_url)

    async def _teams_index(self) -> dict[str, dict[str, Any]]:
        async def factory() -> dict[str, dict[str, Any]]:
            payload = await self.get_json("/teams", params={"per_page": "100"})
            rows = payload.get("data", []) if isinstance(payload, dict) else []
            index: dict[str, dict[str, Any]] = {}
            for row in rows:
                if not isinstance(row, dict):
                    continue
                name = str(row.get("full_name") or row.get("name") or "").strip().lower()
                if name:
                    index[name] = row
                    if row.get("name"):
                        index[str(row["name"]).strip().lower()] = row
            if not index:
                raise SourceError("balldontlie: /teams не вернул команд (проверьте ключ/тариф)")
            return index

        try:
            self._teams = await self._cache.get_or_set("teams", factory, ttl_sec=24 * 3600)
        except Exception as exc:
            logger.debug("balldontlie: индекс команд недоступен ({})", exc)
        return self._teams

    async def find_team(self, name: str) -> dict[str, Any] | None:
        index = await self._teams_index()
        key = name.strip().lower()
        if key in index:
            return index[key]
        for candidate, row in index.items():
            if key in candidate or candidate in key:
                return row
        return None

    async def get_games(self, start: date, end: date, team_id: int | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"start_date": start.isoformat(), "end_date": end.isoformat(), "per_page": "100"}
        if team_id:
            params["team_ids[]"] = str(team_id)
        payload = await self.get_json("/games", params=params)
        rows = payload.get("data", []) if isinstance(payload, dict) else []
        return [row for row in rows if isinstance(row, dict)]

    async def get_recent_form(self, team: str, n: int = 5, **kwargs: Any) -> list[FormEntry]:
        team_row = await self.find_team(team)
        if not team_row:
            return []
        now = datetime.now(timezone.utc)
        games = await self.get_games(now.date() - timedelta(days=90), now.date(), team_id=int(team_row["id"]))
        finished = [game for game in games if game.get("status") == "Final" or game.get("period")]
        finished.sort(key=lambda game: str(game.get("date") or ""), reverse=True)
        entries: list[FormEntry] = []
        for game in finished[:n]:
            home = game.get("home_team") or {}
            visitor = game.get("visitor_team") or {}
            is_home = str(home.get("id")) == str(team_row["id"])
            home_score = game.get("home_team_score")
            away_score = game.get("visitor_team_score")
            if home_score is None or away_score is None:
                continue
            scored = home_score if is_home else away_score
            missed = away_score if is_home else home_score
            entries.append(
                FormEntry(
                    date=_parse_datetime(game.get("date")),
                    opponent=str((visitor if is_home else home).get("full_name") or ""),
                    is_home=is_home,
                    goals_for=int(scored),
                    goals_against=int(missed),
                    result="W" if scored > missed else "L",
                    competition="NBA",
                )
            )
        return entries

    async def get_team_stats(self, team: str, last: int = 10) -> BasketballTeamStats | None:
        """Оценка pace/ORtg/DRtg по счётам: баллдонтли не отдаёт boxscore в v1."""
        team_row = await self.find_team(team)
        if not team_row:
            return None
        now = datetime.now(timezone.utc)
        games = await self.get_games(now.date() - timedelta(days=120), now.date(), team_id=int(team_row["id"]))
        finished = [game for game in games if game.get("home_team_score") is not None]
        finished.sort(key=lambda game: str(game.get("date") or ""), reverse=True)
        if not finished:
            return None
        points_for = points_against = 0.0
        count = 0
        for game in finished[:last]:
            home_score = _to_float(game.get("home_team_score"))
            away_score = _to_float(game.get("visitor_team_score"))
            if home_score is None or away_score is None:
                continue
            is_home = str((game.get("home_team") or {}).get("id")) == str(team_row["id"])
            points_for += home_score if is_home else away_score
            points_against += away_score if is_home else home_score
            count += 1
        if count == 0:
            return None
        ppg_for = points_for / count
        ppg_against = points_against / count
        base_ortg = 114.0
        return BasketballTeamStats(
            team=team,
            pace=settings.basketball_possessions_per_48,
            ortg=round(base_ortg * (ppg_for / LEAGUE_AVG_PPG["nba"]), 2),
            drtg=round(base_ortg * (ppg_against / LEAGUE_AVG_PPG["nba"]), 2),
            games=count,
            source="balldontlie:estimated (ORtg/DRtg по очкам, boxscore не отдаёт) → weak",
        )

    # ---------------------------------------------------------- прочие методы Stats
    async def get_h2h(self, home_team: str, away_team: str, **kwargs: Any) -> list[Any]:
        from app.sources.base import H2HRecord

        team_row = await self.find_team(home_team)
        if not team_row:
            return []
        opponent_key = away_team.strip().lower()
        now = datetime.now(timezone.utc)
        games = await self.get_games(now.date() - timedelta(days=400), now.date(), team_id=int(team_row["id"]))
        records: list[H2HRecord] = []
        for game in games:
            home = game.get("home_team") or {}
            visitor = game.get("visitor_team") or {}
            names = {str(home.get("full_name") or "").lower(), str(visitor.get("full_name") or "").lower()}
            if opponent_key not in names:
                continue
            records.append(
                H2HRecord(
                    date=_parse_datetime(game.get("date")),
                    home_team=str(home.get("full_name") or ""),
                    away_team=str(visitor.get("full_name") or ""),
                    home_score=_int_or_none(game.get("home_team_score")),
                    away_score=_int_or_none(game.get("visitor_team_score")),
                    competition="NBA",
                )
            )
        records.sort(key=lambda record: record.date or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        return records[: settings.h2h_limit]

    async def get_injuries(self, team: str, **kwargs: Any) -> list[Any]:
        return []

    async def get_predicted_lineups(self, team: str, **kwargs: Any) -> Any:
        return None

    async def get_schedule_density(self, team: str, **kwargs: Any) -> ScheduleDensity:
        form = await self.get_recent_form(team, n=10)
        now = datetime.now(timezone.utc)
        last_week = now - timedelta(days=7)
        played = [entry for entry in form if entry.date and last_week <= entry.date <= now]
        last_game = max((entry.date for entry in played), default=None)
        return ScheduleDensity(
            matches_last_7_days=len(played),
            matches_next_7_days=0,
            rest_days=(now - last_game).days if last_game else None,
            back_to_back=bool(last_game and (now - last_game) < timedelta(hours=30)),
            three_in_four=len(played) >= 3,
            notes="balldontlie: по последним играм",
        )

    async def get_upcoming(self, day: date) -> list[Match]:
        """Расписание NBA на дату (используется коллектором как дополнительный источник матчей)."""
        games = await self.get_games(day, day)
        matches: list[Match] = []
        for game in games:
            home = game.get("home_team") or {}
            visitor = game.get("visitor_team") or {}
            moment = _parse_datetime(
                game.get("datetime") or game.get("date")
            ) or datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc)
            matches.append(
                Match(
                    ext_id=f"balldontlie:{game.get('id')}",
                    sport="basketball",
                    league="NBA",
                    home_team=str(home.get("full_name") or ""),
                    away_team=str(visitor.get("full_name") or ""),
                    starts_at=moment,
                    source="balldontlie",
                    status="finished" if game.get("status") == "Final" else "scheduled",
                    home_score=_int_or_none(game.get("home_team_score")),
                    away_score=_int_or_none(game.get("visitor_team_score")),
                )
            )
        return matches
