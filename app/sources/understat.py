"""Understat — бесплатный xG-источник для футбола (Модуль 1).

ВАЖНО (честно):
  * У Understat нет публичного API. Мы разбираем страницу лиги
    https://understat.com/league/<CODE>/<SEASON> и встроенный объект
    `__INITIAL_STATE__` (переменные teamsData / datesData / playersData).
    Это скрейпинг: сайт может изменить разметку — тогда провайдер пишет
    предупреждение и возвращает пусто (сервис продолжает работать).
    Проверить вручную: scripts/check_sources.py.
  * <CODE> ∈ EPL, La_liga, Serie_A, Bundesliga, Ligue_1, RFPL.
  * Из истории команды мы достаём xG/xGA дома и в гостях, а из datesData —
    средние лиги по голам/xG дома и в гостях (вход для Пуассона).

Кэш: 12 часов в памяти (settings.cache_ttl_xg_sec) + запись в таблицу xg_cache.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from loguru import logger

from app.config import settings
from app.sources.base import (
    BaseHttpClient,
    FormEntry,
    H2HRecord,
    ScheduleDensity,
    SourceError,
    StatsProvider,
    TeamXgStats,
    TTLCache,
)

# Соответствие «лига из расписания» → код Understat. Ключи — в нижнем регистре.
LEAGUE_CODES: dict[str, str] = {
    "premier league": "EPL",
    "eng. premier league": "EPL",
    "epl": "EPL",
    "la liga": "La_liga",
    "laliga": "La_liga",
    "serie a": "Serie_A",
    "bundesliga": "Bundesliga",
    "ligue 1": "Ligue_1",
    "rpl": "RFPL",
    "российская премьер": "RFPL",
    "премьер-лига": "RFPL",
}

# Регулярка для встроенных JSON: var name = JSON.parse('....');
_STATE_RE = re.compile(r"var\s+(?P<name>\w+)\s*=\s*JSON\.parse\('(?P<payload>.*?)'\)\s*;?", re.DOTALL)


def current_season_year(now: datetime | None = None) -> int:
    """Год начала сезона (европейские чемпионаты стартуют в августе)."""
    moment = now or datetime.now(timezone.utc)
    return moment.year if moment.month >= 7 else moment.year - 1


def league_code_for(league: str) -> str | None:
    """Название лиги → код Understat (или None, если лига не поддерживается)."""
    low = (league or "").lower()
    for key, code in LEAGUE_CODES.items():
        if key in low:
            return code
    return None


def decode_initial_state(payload: str) -> Any:
    """Раскодировать JSON из JS-строки Understat (экранированный unicode)."""
    # В JS-строке символы закодированы как \x22, \u00e9 и т.п.
    decoded = payload.encode("utf-8").decode("unicode_escape")
    return json.loads(decoded)


def parse_initial_state(html: str) -> dict[str, Any]:
    """HTML страницы лиги → {'teamsData': {...}, 'datesData': [...], ...}."""
    state: dict[str, Any] = {}
    for match in _STATE_RE.finditer(html or ""):
        try:
            state[match.group("name")] = decode_initial_state(match.group("payload"))
        except (ValueError, json.JSONDecodeError) as exc:
            logger.debug("understat: не удалось разобрать {} ({})", match.group("name"), exc)
    if not state:
        logger.warning("understat: __INITIAL_STATE__ не найден — изменилась разметка страницы лиги")
    return state


def _float(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _mean(values: list[float]) -> float | None:
    clean = [value for value in values if value is not None]
    return round(sum(clean) / len(clean), 3) if clean else None


@dataclass
class UnderstatLeague:
    """Разобранная страница лиги: команды + средние лиги + матчи (datesData)."""

    code: str
    season: int
    teams: dict[str, TeamXgStats] = field(default_factory=dict)
    league_avg_home: float = 0.0
    league_avg_away: float = 0.0
    matches: list[dict[str, Any]] = field(default_factory=list)
    parsed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def team_stats(self, name: str) -> TeamXgStats | None:
        key = (name or "").strip().lower()
        if not key:
            return None
        if key in self.teams:
            return self.teams[key]
        for team_key, stats in self.teams.items():
            if key in team_key or team_key in key:
                return stats
        return None


class UnderstatProvider(StatsProvider):
    """xG футбольных лиг (скрейпинг Understat, кэш 12ч + таблица xg_cache)."""

    provider_name = "understat"
    sport = "football"

    def __init__(self, base_url: str | None = None) -> None:
        super().__init__(
            source_name="understat",
            base_url=base_url or settings.understat_base_url,
            politeness=True,
        )
        self._cache = TTLCache(ttl_sec=settings.cache_ttl_xg_sec)

    @property
    def available(self) -> bool:
        return bool(self.base_url)

    # ------------------------------------------------------------------ league
    async def fetch_league(self, code: str, season: int | None = None) -> UnderstatLeague:
        """Скачивает и разбирает страницу лиги (кэш 12ч)."""

        async def factory() -> UnderstatLeague:
            payload = await self._fetch_league_uncached(code, season)
            return payload

        return await self._cache.get_or_set(
            f"league:{code}:{season or current_season_year()}", factory, ttl_sec=settings.cache_ttl_xg_sec
        )

    async def _fetch_league_uncached(self, code: str, season: int | None) -> UnderstatLeague:
        years = [season] if season else [current_season_year(), current_season_year() - 1]
        for year in years:
            url = f"/league/{code}/{year}"
            try:
                html = await self.get_text(url, polite=True)
            except Exception as exc:
                logger.warning("understat: {} недоступен ({})", url, exc)
                continue
            state = parse_initial_state(html)
            teams_raw = state.get("teamsData")
            if not teams_raw:
                logger.warning("understat: на странице {} нет teamsData", url)
                continue
            league = self._build_league(code, year, teams_raw, state.get("datesData") or [])
            if len(league.teams) >= 4:
                logger.info(
                    "understat: {} {} — {} команд, средние лиги {:.2f}/{:.2f}",
                    code, year, len(league.teams), league.league_avg_home, league.league_avg_away,
                )
                return league
            logger.warning("understat: {} вернул лишь {} команд — пробую другой сезон", url, len(league.teams))
        logger.error(
            "understat: не удалось получить данные лиги {} (проверьте https://understat.com/league/{}/ вручную)",
            code, code,
        )
        return UnderstatLeague(code=code, season=season or current_season_year())

    @staticmethod
    def _build_league(code: str, season: int, teams_raw: dict[str, Any], dates: list[dict[str, Any]]) -> UnderstatLeague:
        teams: dict[str, TeamXgStats] = {}
        for team_id, team_payload in teams_raw.items():
            if not isinstance(team_payload, dict):
                continue
            title = str(team_payload.get("title") or "").strip()
            history = team_payload.get("history") or []
            if not title or not isinstance(history, list):
                continue
            home_xg: list[float] = []
            home_xga: list[float] = []
            away_xg: list[float] = []
            away_xga: list[float] = []
            overall_xg: list[float] = []
            overall_xga: list[float] = []
            for entry in history:
                if not isinstance(entry, dict):
                    continue
                xg = _float(entry.get("xG"))
                xga = _float(entry.get("xGA"))
                overall_xg.append(xg) if xg is not None else None
                overall_xga.append(xga) if xga is not None else None
                side = str(entry.get("h_a") or "").lower()
                if side == "h":
                    home_xg.append(xg) if xg is not None else None
                    home_xga.append(xga) if xga is not None else None
                elif side == "a":
                    away_xg.append(xg) if xg is not None else None
                    away_xga.append(xga) if xga is not None else None
            if not overall_xg and not overall_xga:
                continue
            # Если сплитов ещё нет (начало сезона) — используем общие средние (честный фолбэк).
            fallback_for = _mean(overall_xg)
            fallback_against = _mean(overall_xga)
            teams[title.lower()] = TeamXgStats(
                team=title,
                matches=len(overall_xg) or len(overall_xga),
                xg_for_per_game=fallback_for,
                xg_against_per_game=fallback_against,
                xg_for_home=_mean(home_xg) or fallback_for,
                xg_against_home=_mean(home_xga) or fallback_against,
                xg_for_away=_mean(away_xg) or fallback_for,
                xg_against_away=_mean(away_xga) or fallback_against,
                source=f"understat:{code}:{season}",
            )

        # Средние лиги — из фактических матчей datesData (голы/xG хозяев и гостей).
        home_values: list[float] = []
        away_values: list[float] = []
        for match in dates:
            if not isinstance(match, dict) or not match.get("isResult"):
                continue
            xg = match.get("xG") or {}
            goals = match.get("goals") or {}
            home_value = _float(xg.get("h"))
            away_value = _float(xg.get("a"))
            if home_value is None:
                home_value = _float(goals.get("h"))
            if away_value is None:
                away_value = _float(goals.get("a"))
            if home_value is not None:
                home_values.append(home_value)
            if away_value is not None:
                away_values.append(away_value)

        league_avg_home = _mean(home_values) or 1.55
        league_avg_away = _mean(away_values) or 1.20
        return UnderstatLeague(
            code=code,
            season=season,
            teams=teams,
            league_avg_home=league_avg_home,
            league_avg_away=league_avg_away,
            matches=[match for match in dates if isinstance(match, dict)],
        )

    # ------------------------------------------------------------ команда/лига
    async def get_team_xg(self, team: str, code: str, season: int | None = None) -> TeamXgStats | None:
        """xG команды в лиге кодом `code` (например, "EPL")."""
        league = await self.fetch_league(code, season)
        return league.team_stats(team)

    async def get_league_averages(self, code: str, season: int | None = None) -> tuple[float, float]:
        league = await self.fetch_league(code, season)
        return league.league_avg_home, league.league_avg_away

    async def get_team_xg_by_league_name(self, team: str, league_name: str) -> tuple[TeamXgStats | None, str | None]:
        """Удобная обёртка для pipeline: сам определяет код лиги по названию."""
        code = league_code_for(league_name)
        if code is None:
            return None, None
        stats = await self.get_team_xg(team, code)
        return stats, code

    # ---------------------------------------------------- контракт StatsProvider
    async def get_injuries(self, team: str, **kwargs: Any) -> list[Any]:
        """Understat травмы не публикует."""
        return []

    async def get_predicted_lineups(self, team: str, **kwargs: Any) -> Any:
        return None

    async def get_recent_form(self, team: str, n: int = 5, **kwargs: Any) -> list[FormEntry]:
        """Последние матчи команды из datesData (xG/xGA/счёт)."""
        code = kwargs.get("code") or league_code_for(kwargs.get("league") or "")
        if not code:
            return []
        league = await self.fetch_league(code)
        key = (team or "").strip().lower()
        entries: list[tuple[datetime, dict[str, Any], bool]] = []
        for match in league.matches:
            if not match.get("isResult"):
                continue
            home = match.get("h") or {}
            away = match.get("a") or {}
            home_title = str(home.get("title") or "").lower()
            away_title = str(away.get("title") or "").lower()
            is_home = key and (key in home_title or home_title in key)
            is_away = key and (key in away_title or away_title in key)
            if not is_home and not is_away:
                continue
            moment = _parse_datetime(match.get("datetime"))
            if moment:
                entries.append((moment, match, bool(is_home)))
        entries.sort(key=lambda item: item[0], reverse=True)

        form: list[FormEntry] = []
        for moment, match, is_home in entries[:n]:
            goals = match.get("goals") or {}
            xg = match.get("xG") or {}
            scored = _float(goals.get("h") if is_home else goals.get("a"))
            missed = _float(goals.get("a") if is_home else goals.get("h"))
            opponent = (match.get("a") if is_home else match.get("h")) or {}
            result = None
            if scored is not None and missed is not None:
                result = "W" if scored > missed else ("D" if scored == missed else "L")
            form.append(
                FormEntry(
                    date=moment,
                    opponent=str(opponent.get("title") or ""),
                    is_home=is_home,
                    goals_for=int(scored) if scored is not None else None,
                    goals_against=int(missed) if missed is not None else None,
                    xg=_float(xg.get("h") if is_home else xg.get("a")),
                    xga=_float(xg.get("a") if is_home else xg.get("h")),
                    result=result,
                    competition=code,
                )
            )
        return form

    async def get_h2h(self, home_team: str, away_team: str, **kwargs: Any) -> list[H2HRecord]:
        """Очные матчи этого сезона из datesData (историю прошлых сезонов Understat не отдаёт)."""
        code = kwargs.get("code") or league_code_for(kwargs.get("league") or "")
        if not code:
            return []
        league = await self.fetch_league(code)
        home_key = (home_team or "").strip().lower()
        away_key = (away_team or "").strip().lower()
        records: list[H2HRecord] = []
        for match in league.matches:
            if not match.get("isResult"):
                continue
            home = match.get("h") or {}
            away = match.get("a") or {}
            home_title = str(home.get("title") or "")
            away_title = str(away.get("title") or "")
            pair = {home_title.lower(), away_title.lower()}
            if not ({home_key, away_key} & pair) or len({home_key, away_key} & pair) < 2:
                continue
            goals = match.get("goals") or {}
            records.append(
                H2HRecord(
                    date=_parse_datetime(match.get("datetime")),
                    home_team=home_title,
                    away_team=away_title,
                    home_score=int(_float(goals.get("h")) or 0),
                    away_score=int(_float(goals.get("a")) or 0),
                    competition=code,
                )
            )
        records.sort(key=lambda record: record.date or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        return records[: settings.h2h_limit]

    async def get_schedule_density(self, team: str, **kwargs: Any) -> ScheduleDensity:
        """Плотность календаря: матчи за/в следующие 7 дней + отдых (по datesData лиги)."""
        code = kwargs.get("code") or league_code_for(kwargs.get("league") or "")
        if not code:
            return ScheduleDensity(notes="understat: лига не определена")
        league = await self.fetch_league(code)
        key = (team or "").strip().lower()
        now = datetime.now(timezone.utc)
        last_week = now - timedelta(days=7)
        next_week = now + timedelta(days=7)
        last_count = next_count = 0
        last_played: datetime | None = None
        for match in league.matches:
            home_title = str((match.get("h") or {}).get("title") or "").lower()
            away_title = str((match.get("a") or {}).get("title") or "").lower()
            if not key or (key not in home_title and key not in away_title and home_title not in key and away_title not in key):
                continue
            moment = _parse_datetime(match.get("datetime"))
            if moment is None:
                continue
            if match.get("isResult") and last_week <= moment <= now:
                last_count += 1
                if last_played is None or moment > last_played:
                    last_played = moment
            elif not match.get("isResult") and now < moment <= next_week:
                next_count += 1
        return ScheduleDensity(
            matches_last_7_days=last_count,
            matches_next_7_days=next_count,
            rest_days=(now - last_played).days if last_played else None,
            back_to_back=False,  # в футболе нет back-to-back в смысле НХЛ
            notes="understat: по календарю лиги",
        )

    # ------------------------------------------------------------- кэш в БД
    async def save_to_db(self, session: Any, team_id: int, stats: TeamXgStats, code: str, season: int) -> None:
        """Пишет xG команды в xg_cache (source='understat:<code>:<season>')."""
        from app.sources.team_matching import upsert_xg_cache

        await upsert_xg_cache(
            session,
            team_id=team_id,
            source=self.cache_source(code, season),
            payload=stats.model_dump(),
        )

    async def load_from_db(self, session: Any, team_id: int, code: str, season: int) -> TeamXgStats | None:
        """Читает xG из xg_cache, если запись свежее cache_ttl_xg_sec."""
        from app.sources.team_matching import load_xg_cache

        payload = await load_xg_cache(
            session,
            team_id=team_id,
            source=self.cache_source(code, season),
            max_age_sec=settings.cache_ttl_xg_sec,
        )
        if not payload:
            return None
        try:
            return TeamXgStats.model_validate(payload)
        except Exception as exc:
            logger.debug("understat: битый payload в xg_cache (team_id={}, {}): {}", team_id, code, exc)
            return None

    @staticmethod
    def cache_source(code: str, season: int) -> str:
        return f"understat:{code}:{season}"[:40]


def _parse_datetime(raw: Any) -> datetime | None:
    if not raw:
        return None
    text = str(raw).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None
