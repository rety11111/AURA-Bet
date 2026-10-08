"""MoneyPuck — бесплатные CSV с xG-статистикой НХЛ (Модуль 1, хоккей).

ВАЖНО (честно, проверяйте scripts/check_sources.py):
  * MoneyPuck не имеет официального API — это публичные CSV-файлы, которые сайт
    складывает в /moneypuck/playerData/seasonSummary/<season>/<phase>/{teams|goalies}.csv,
    где <phase> ∈ {regular, playoffs, all}, <season> — год начала сезона (2025 → 2025/26).
    Шаблон URL и набор колонок МОГУТ измениться без предупреждения — парсер
    толерантен к именам колонок и логирует, чего не нашёл, вместо падения.
  * В командном CSV нет разбивки home/away (только общие xG за/против на игру).
    Поэтому домашнее преимущество НХЛ учитывается моделью Пуассона
    (hockey_home_advantage), а не сплитами MoneyPuck. Это зафиксировано здесь
    и в SETUP.md.
  * Если сезон в файле ещё не появился (начало чемпионата), провайдер пробует
    предыдущие сезоны — с явным логом.

Что берём: xG за/против на игру (situation="all") → TeamXgStats для Пуассона,
а также лидеров вратарей (xG против + GSAA) для контекста LLM.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime, timezone
from typing import Any

from loguru import logger

from app.config import settings
from app.sources.base import BaseHttpClient, SourceError, StatsProvider, TeamXgStats, TTLCache

XG_CACHE_TTL_SEC = 12 * 3600
SEASON_FALLBACK_DEPTH = 3
PHASES = ("regular", "all", "playoffs")

# Возможные имена колонок в CSV (MoneyPuck переименовывает их между сезонами).
COLUMN_CANDIDATES: dict[str, tuple[str, ...]] = {
    "team": ("team", "Team", "name"),
    "games": ("games_played", "gamesPlayed", "gp", "Games Played"),
    "xg_for": ("xGoalsFor", "xgoalsFor", "xg_for", "xGoals"),
    "xg_against": ("xGoalsAgainst", "xgoalsAgainst", "xg_against", "xGoalsAg"),
    "goals_for": ("goalsFor", "goals_for", "GF"),
    "goals_against": ("goalsAgainst", "goals_against", "GA"),
    "situation": ("situation", "Situation"),
    "goalie": ("name", "player", "Player", "goalie"),
    "goalie_games": ("games_played", "gamesPlayed", "GP"),
    "goalie_xga": ("xGoalsAgainst", "xgoalsAgainst", "xGA"),
    "goalie_gsaa": ("gsaa", "GSAA", "goalsSavedAboveExpected", "GSAx"),
}


def current_season_year(now: datetime | None = None) -> int:
    """Год начала текущего сезона НХЛ: с июля считаем новым сезоном."""
    moment = now or datetime.now(timezone.utc)
    return moment.year if moment.month >= 7 else moment.year - 1


def season_candidates(now: datetime | None = None) -> list[int]:
    start = current_season_year(now)
    return [start - offset for offset in range(SEASON_FALLBACK_DEPTH)]


def _pick(row: dict[str, Any], key: str) -> Any:
    for candidate in COLUMN_CANDIDATES.get(key, ()):
        if candidate in row:
            return row[candidate]
    return None


def _to_float(value: Any) -> float | None:
    try:
        if value in (None, "", "NaN"):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> int | None:
    number = _to_float(value)
    return int(number) if number is not None else None


def parse_csv_rows(text: str) -> list[dict[str, Any]]:
    """CSV → список словарей. Пустой/битый файл → SourceError."""
    if not text or "<html" in text[:200].lower():
        raise SourceError("moneypuck: получен не CSV (возможно, страница-заглушка)")
    reader = csv.DictReader(io.StringIO(text))
    rows = [{k.strip(): v for k, v in row.items() if k} for row in reader]
    if not rows:
        raise SourceError("moneypuck: CSV без данных")
    return rows


def parse_team_xg(rows: list[dict[str, Any]], team_name: str | None = None) -> dict[str, TeamXgStats]:
    """Командный CSV → {lowercase team: TeamXgStats}. Берём situation='all' (иначе первую строку на команду)."""
    best: dict[str, dict[str, Any]] = {}
    for row in rows:
        team = _pick(row, "team")
        if not team:
            continue
        key = str(team).strip().lower()
        situation = str(_pick(row, "situation") or "all").lower()
        if key in best and situation != "all":
            best[key].setdefault("_situations", set()).add(situation) if isinstance(best[key].get("_situations"), set) else None
            continue
        if key in best and situation == "all":
            pass  # ситуация all перекрывает остальные
        elif key in best and best[key].get("_situation") == "all":
            continue
        row = dict(row)
        row["_situation"] = situation
        best[key] = row

    result: dict[str, TeamXgStats] = {}
    for key, row in best.items():
        games = _to_int(_pick(row, "games")) or 0
        xg_for = _to_float(_pick(row, "xg_for"))
        xg_against = _to_float(_pick(row, "xg_against"))
        goals_for = _to_float(_pick(row, "goals_for"))
        goals_against = _to_float(_pick(row, "goals_against"))
        if xg_for is None and goals_for is None:
            continue
        if xg_for is None or xg_against is None:
            # xG нет — используем голы (честный фолбэк, помечаем в source)
            xg_for, xg_against = goals_for, goals_against
            quality = "goals (фолбэк: xG-колонок нет в CSV)"
        else:
            quality = "xG"
        if games <= 0 or xg_for is None or xg_against is None:
            continue
        result[key] = TeamXgStats(
            team=str(_pick(row, "team")),
            matches=games,
            # Разбивки home/away в MoneyPuck нет — оставляем None (см. docstring модуля)
            xg_for_per_game=round(xg_for / games, 3),
            xg_against_per_game=round(xg_against / games, 3),
            xg_for_home=None,
            xg_against_home=None,
            xg_for_away=None,
            xg_against_away=None,
            source=f"moneypuck:{quality}",
        )
    if team_name:
        found = result.get(team_name.strip().lower())
        return {team_name.strip().lower(): found} if found else {}
    return result


def league_averages(teams: dict[str, TeamXgStats]) -> tuple[float, float]:
    """Средние xG за/против на команду в лиге (на игру) — вход Пуассона.

    Это НЕ home/away-сплиты (MoneyPuck их не отдаёт) — обе величины равны средней
    результативности одной команды за матч.
    """
    if not teams:
        return 3.0, 2.8
    scored = [stat.xg_for_per_game for stat in teams.values() if stat.xg_for_per_game]
    conceded = [stat.xg_against_per_game for stat in teams.values() if stat.xg_against_per_game]
    return (
        round(sum(scored) / len(scored), 3) if scored else 3.0,
        round(sum(conceded) / len(conceded), 3) if conceded else 2.8,
    )


def parse_goalie_leaders(rows: list[dict[str, Any]], team_name: str | None = None, limit: int = 3) -> list[dict[str, Any]]:
    """Вратари команды с GSAA/GSAx (контекст для LLM: надёжность вратаря)."""
    if team_name:
        rows = [row for row in rows if str(_pick(row, "team") or "").strip().lower() == team_name.strip().lower()]
    keepers: list[dict[str, Any]] = []
    for row in rows:
        name = _pick(row, "goalie")
        if not name:
            continue
        situation = str(_pick(row, "situation") or "all").lower()
        if situation not in ("all", "5on5", "5v5"):
            continue
        keepers.append(
            {
                "goalie": str(name),
                "games": _to_int(_pick(row, "goalie_games")),
                "xga": _to_float(_pick(row, "goalie_xga")),
                "gsaa": _to_float(_pick(row, "goalie_gsaa")),
                "situation": situation,
            }
        )
    keepers.sort(key=lambda item: (item.get("gsaa") or -999), reverse=True)
    return keepers[:limit]


class MoneyPuckProvider(StatsProvider):
    """xG-статистика хоккея (MoneyPuck CSV)."""

    provider_name = "moneypuck"
    sport = "hockey"

    def __init__(self, base_url: str | None = None) -> None:
        super().__init__(
            source_name="moneypuck",
            base_url=base_url or settings.moneypuck_base_url,
            politeness=True,  # это сайт, а не API — держим паузы из settings.scrape_delay_*
        )
        self._cache = TTLCache(ttl_sec=XG_CACHE_TTL_SEC)
        self._memory_teams: dict[str, TeamXgStats] = {}

    @property
    def available(self) -> bool:
        return bool(self.base_url)

    # ------------------------------------------------------------------- fetch
    async def _fetch_csv(self, name: str, season: int, phase: str = "regular") -> list[dict[str, Any]]:
        """name = teams | goalies. Кэш в памяти 12ч (файлы большие, а лимитов у сайта нет только снаружи)."""
        url = f"/seasonSummary/{season}/{phase}/{name}.csv"

        async def factory() -> list[dict[str, Any]]:
            text = await self.get_text(url, polite=True)
            return parse_csv_rows(text)

        return await self._cache.get_or_set(f"{name}:{season}:{phase}", factory, ttl_sec=settings.cache_ttl_xg_sec)

    async def load_teams(self, season: int | None = None, phase: str = "regular") -> dict[str, TeamXgStats]:
        """Команды сезона. Если данных нет — идём по предыдущим сезонам (с логом)."""
        candidates = [season] if season else season_candidates()
        for year in candidates:
            try:
                rows = await self._fetch_csv("teams", year, phase)
            except Exception as exc:
                logger.warning("moneypuck: teams.csv сезона {} недоступен ({})", year, exc)
                continue
            teams = parse_team_xg(rows)
            if teams:
                self._memory_teams = teams
                if year != (season or current_season_year()):
                    logger.info("moneypuck: сезон {} ещё не опубликован, использую {}", season or current_season_year(), year)
                return teams
            logger.warning("moneypuck: teams.csv сезона {} пуст — пробую предыдущий", year)
        logger.error(
            "moneypuck: не удалось получить xG-статистику ни за один сезон. "
            "Проверьте URL вручную: {}/seasonSummary/<season>/regular/teams.csv",
            settings.moneypuck_base_url,
        )
        return {}

    async def get_team_xg(self, team: str, season: int | None = None) -> TeamXgStats | None:
        """TeamXgStats для команды по названию (без учёта регистра)."""
        key = (team or "").strip().lower()
        if not key:
            return None
        if key in self._memory_teams:
            return self._memory_teams[key]
        teams = await self.load_teams(season)
        if key in teams:
            return teams[key]
        # Точное имя не совпало — пробуем мягкий поиск по подстроке (MoneyPuck пишет «Tampa Bay Lightning»)
        for name, stats in teams.items():
            if key in name or name in key:
                return stats
        logger.debug("moneypuck: команда '{}' не найдена в CSV сезона", team)
        return None

    async def get_league_averages(self, season: int | None = None, phase: str = "regular") -> tuple[float, float]:
        """(xG за команду за игру, xG против команды за игру) по лиге."""
        teams = await self.load_teams(season, phase)
        return league_averages(teams)

    async def get_goalie_stats(self, team: str, season: int | None = None) -> list[dict[str, Any]]:
        """Вратари команды (кэш 12ч), отсортированы по GSAA."""
        year = season or current_season_year()
        for candidate in ([year] if season else season_candidates()):
            try:
                rows = await self._fetch_csv("goalies", candidate, "regular")
            except Exception as exc:
                logger.debug("moneypuck: goalies.csv сезона {} недоступен ({})", candidate, exc)
                continue
            leaders = parse_goalie_leaders(rows, team_name=team)
            if leaders:
                return leaders
        return []

    # --------------------------------------------- контракт StatsProvider (минимум)
    async def get_injuries(self, team: str, **kwargs: Any) -> list[Any]:
        """MoneyPuck травмы не отдаёт — возвращаем пусто (деградация, см. collector)."""
        return []

    async def get_predicted_lineups(self, team: str, **kwargs: Any) -> Any:
        return None

    async def get_recent_form(self, team: str, n: int = 5, **kwargs: Any) -> list[Any]:
        """Форма — это к NHLStats API; MoneyPuck только агрегаты. Пусто → модель работает по xG."""
        return []

    async def get_h2h(self, home_team: str, away_team: str, **kwargs: Any) -> list[Any]:
        return []

    async def get_schedule_density(self, team: str, **kwargs: Any) -> Any:
        from app.sources.base import ScheduleDensity

        return ScheduleDensity(notes="moneypuck: календарь не отдаёт (см. NHL provider)")

    # ------------------------------------------------------------ кэш в БД (xg_cache)
    async def save_to_db(self, session: Any, team_id: int, stats: TeamXgStats) -> None:
        """Сохраняет xG команды в xg_cache (source='moneypuck'), чтобы не дёргать CSV."""
        from app.sources.team_matching import upsert_xg_cache

        await upsert_xg_cache(session, team_id=team_id, source="moneypuck", payload=stats.model_dump())

    async def load_from_db(self, session: Any, team_id: int) -> TeamXgStats | None:
        """Читает xG из xg_cache, если запись свежее cache_ttl_xg_sec."""
        from app.sources.team_matching import load_xg_cache

        payload = await load_xg_cache(session, team_id=team_id, source="moneypuck", max_age_sec=settings.cache_ttl_xg_sec)
        if not payload:
            return None
        try:
            return TeamXgStats.model_validate(payload)
        except Exception as exc:
            logger.debug("moneypuck: битый payload в xg_cache для team_id={} ({})", team_id, exc)
            return None
