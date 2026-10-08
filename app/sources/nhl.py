"""NHL Stats API — бесплатный официальный источник статистики НХЛ (Модуль 1).

ВАЖНО (честно):
  * host = https://api-web.nhle.com/v1 — это актуальный публичный API НХЛ (тот,
    который использует сайт nhl.com). Старый statsapi.web.nhle.com/api/v1 закрыт.
    Хост задаётся в .env: NHL_API_BASE (см. .env.example).
  * «Вероятный стартовый вратарь» в API не публикуется как отдельное поле. Мы
    ВЫЧИСЛЯЕМ его из фактических стартов последних матчей (/club-schedule-season
    + /gamecenter/{id}/boxscore): берём вратаря, который не играл вчера, а до этого
    стартовал больше всех. Это эвристика, а не официальное поле — проверяйте
    scripts/check_sources.py.

Использование в pipeline:
  * get_recent_form / get_h2h / get_schedule_density — контекст для LLM-анализа;
  * get_team_goal_stats — фолбэк для Пуассона (голы за/против на матч), если
    MoneyPuck (xG) недоступен.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from difflib import SequenceMatcher
from typing import Any

from loguru import logger
from rapidfuzz import fuzz, process

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

# Допустимые в NHL аббревиатуры команд нужны, чтобы отделять название команды от
# служебных сегментов URL. Кэш `/standings/now` даёт полный список (см. _teams_index).
STANDINGS_TTL_SEC = 6 * 3600
SCHEDULE_TTL_SEC = 900
BOXSCORE_TTL_SEC = 3600
# Матчи, которые считать «сегодня», для формы/плотности календаря
FORM_WINDOW_DAYS = 90


def _int(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_date(raw: Any) -> datetime | None:
    if not raw:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    text = str(raw).strip()
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            parsed = datetime.strptime(text.replace("Z", "+0000"), fmt)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    logger.debug("nhl: не удалось распарсить дату {}", raw)
    return None


class NhlProvider(StatsProvider):
    """Провайдер статистики НХЛ (хоккей)."""

    provider_name = "nhl"
    sport = "hockey"

    def __init__(self, base_url: str | None = None) -> None:
        super().__init__(
            source_name="nhl",
            base_url=base_url or settings.nhl_api_base,
            headers={"Accept": "application/json"},
        )
        self._cache = TTLCache(ttl_sec=SCHEDULE_TTL_SEC)
        self._teams_cache = TTLCache(ttl_sec=STANDINGS_TTL_SEC)
        self._boxscore_cache = TTLCache(ttl_sec=BOXSCORE_TTL_SEC)

    @property
    def available(self) -> bool:
        return bool(self.base_url)

    # ------------------------------------------------------------- team index
    async def _teams_index(self) -> dict[str, str]:
        """{'toronto maple leafs': 'TOR', ...} из /standings/now (кэш 6ч)."""

        async def factory() -> dict[str, str]:
            payload = await self.get_json("/standings/now")
            index: dict[str, str] = {}
            for row in payload.get("standings", []) if isinstance(payload, dict) else []:
                abbrev = (
                    (row.get("teamAbbrev") or {}).get("default")
                    if isinstance(row.get("teamAbbrev"), dict)
                    else row.get("teamAbbrev")
                )
                name = ((row.get("teamName") or {}).get("default")) if isinstance(row.get("teamName"), dict) else None
                common = (
                    (row.get("teamCommonName") or {}).get("default")
                    if isinstance(row.get("teamCommonName"), dict)
                    else None
                )
                place = ((row.get("teamPlaceName") or {}).get("default")) if isinstance(row.get("teamPlaceName"), dict) else None
                if not abbrev:
                    continue
                for candidate in (f"{place} {common}" if place and common else None, name, common):
                    if candidate:
                        index[str(candidate).strip().lower()] = str(abbrev)
            if not index:
                raise SourceError("nhl: /standings/now не дал списка команд (изменилась схема?)")
            return index

        try:
            return await self._teams_cache.get_or_set("teams", factory)
        except Exception as exc:  # деградация: без индекса работаем по аббревиатурам
            logger.warning("nhl: индекс команд недоступен ({}), работаю по аббревиатурам", exc)
            return {}

    async def resolve_team(self, team: str) -> str | None:
        """Название команды → аббревиатура NHL (TOR, BOS, ...)."""
        text = (team or "").strip()
        if not text:
            return None
        if 2 <= len(text) <= 4 and text.isupper() and text.isalpha():
            return text
        index = await self._teams_index()
        if not index:
            return None
        key = text.lower()
        if key in index:
            return index[key]
        match = process.extractOne(key, list(index.keys()), scorer=fuzz.WRatio, score_cutoff=settings.team_match_threshold)
        if match:
            return index[match[0]]
        # Частая ситуация: «Toronto Maple Leafs» против «Maple Leafs» → сравним по подстроке.
        for name, abbrev in index.items():
            if SequenceMatcher(None, key, name).ratio() > 0.85 or key in name or name in key:
                return abbrev
        return None

    # ------------------------------------------------------------- schedule
    async def _club_schedule(self, team: str, season: str = "now") -> list[dict[str, Any]]:
        abbrev = await self.resolve_team(team)
        if not abbrev:
            logger.debug("nhl: не удалось определить аббревиатуру для '{}'", team)
            return []

        async def factory() -> list[dict[str, Any]]:
            payload = await self.get_json(f"/club-schedule-season/{abbrev}/{season}")
            games = payload.get("games", []) if isinstance(payload, dict) else []
            return [game for game in games if isinstance(game, dict)]

        try:
            return await self._cache.get_or_set(f"schedule:{abbrev}:{season}", factory)
        except Exception as exc:
            logger.warning("nhl: расписание {} недоступно ({})", abbrev, exc)
            return []

    @staticmethod
    def _game_result(game: dict[str, Any], abbrev: str) -> tuple[int | None, int | None, bool | None]:
        """(голы команды, голы соперника, играл ли дома) для конкретной команды."""
        home = game.get("homeTeam") or {}
        away = game.get("awayTeam") or {}
        home_abbrev = ((home.get("abbrev") or home.get("commonName") or {}) or {})
        if isinstance(home_abbrev, dict):
            home_abbrev = home_abbrev.get("default")
        away_abbrev = ((away.get("abbrev") or away.get("commonName") or {}) or {})
        if isinstance(away_abbrev, dict):
            away_abbrev = away_abbrev.get("default")
        if home_abbrev == abbrev:
            return _int(home.get("score")), _int(away.get("score")), True
        if away_abbrev == abbrev:
            return _int(away.get("score")), _int(home.get("score")), False
        return None, None, None

    @staticmethod
    def _is_finished(game: dict[str, Any]) -> bool:
        state = (game.get("gameState") or "").upper()
        if state in ("FINAL", "OFF"):
            return True
        return state.startswith("FINAL")

    # ---------------------------------------------------- интерфейс StatsProvider
    async def get_recent_form(self, team: str, n: int = 5, **kwargs: Any) -> list[FormEntry]:
        abbrev = await self.resolve_team(team)
        if not abbrev:
            return []
        edge = datetime.now(timezone.utc) - timedelta(days=FORM_WINDOW_DAYS)
        finished: list[tuple[datetime, dict[str, Any]]] = []
        for game in await self._club_schedule(abbrev):
            if not self._is_finished(game):
                continue
            moment = _parse_date(game.get("gameDate") or game.get("startTimeUTC"))
            if moment is None or moment < edge:
                continue
            finished.append((moment, game))
        finished.sort(key=lambda item: item[0], reverse=True)

        entries: list[FormEntry] = []
        for moment, game in finished[:n]:
            goals_for, goals_against, is_home = self._game_result(game, abbrev)
            if goals_for is None or goals_against is None:
                continue
            result = "W" if goals_for > goals_against else ("D" if goals_for == goals_against else "L")
            opponent = self._opponent_name(game, abbrev)
            entries.append(
                FormEntry(
                    date=moment,
                    opponent=opponent,
                    is_home=is_home,
                    goals_for=goals_for,
                    goals_against=goals_against,
                    result=result,
                    competition="NHL",
                )
            )
        return entries

    @staticmethod
    def _opponent_name(game: dict[str, Any], abbrev: str) -> str | None:
        for side in ("homeTeam", "awayTeam"):
            team = game.get(side) or {}
            value = team.get("abbrev")
            if isinstance(value, dict):
                value = value.get("default")
            if value and value != abbrev:
                name = team.get("commonName") or team.get("name")
                if isinstance(name, dict):
                    name = name.get("default")
                return str(name or value)
        return None

    async def get_h2h(self, home_team: str, away_team: str, **kwargs: Any) -> list[H2HRecord]:
        home_abbrev = await self.resolve_team(home_team)
        away_abbrev = await self.resolve_team(away_team)
        if not home_abbrev or not away_abbrev:
            return []
        edge = datetime.now(timezone.utc) - timedelta(days=3 * 365)
        records: list[H2HRecord] = []
        for game in await self._club_schedule(home_abbrev, season=kwargs.get("season", "now")):
            moment = _parse_date(game.get("gameDate") or game.get("startTimeUTC"))
            home = game.get("homeTeam") or {}
            away = game.get("awayTeam") or {}
            home_code = home.get("abbrev")
            away_code = away.get("abbrev")
            if isinstance(home_code, dict):
                home_code = home_code.get("default")
            if isinstance(away_code, dict):
                away_code = away_code.get("default")
            if {home_code, away_code} != {home_abbrev, away_abbrev}:
                continue
            if moment is None or moment < edge or not self._is_finished(game):
                continue
            records.append(
                H2HRecord(
                    date=moment,
                    home_team=str(home_code),
                    away_team=str(away_code),
                    home_score=_int(home.get("score")),
                    away_score=_int(away.get("score")),
                    competition="NHL",
                )
            )
        records.sort(key=lambda record: record.date or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        return records[: settings.h2h_limit]

    async def get_schedule_density(self, team: str, **kwargs: Any) -> ScheduleDensity:
        abbrev = await self.resolve_team(team)
        if not abbrev:
            return ScheduleDensity(notes="nhl: команда не распознана")
        now = datetime.now(timezone.utc)
        last_week = now - timedelta(days=7)
        next_week = now + timedelta(days=7)
        matches_last = matches_next = 0
        last_game: datetime | None = None
        for game in await self._club_schedule(abbrev):
            moment = _parse_date(game.get("gameDate") or game.get("startTimeUTC"))
            if moment is None or not self._is_finished(game):
                continue
            if last_week <= moment <= now:
                matches_last += 1
            if moment > last_game or last_game is None:
                last_game = moment

        # Ближайшие матчи берём из расписания «now» (оно содержит и будущие игры сезона)
        for game in await self._club_schedule(abbrev):
            moment = _parse_date(game.get("gameDate") or game.get("startTimeUTC"))
            if moment and now < moment <= next_week:
                matches_next += 1

        rest_days = (now - last_game).days if last_game else None
        return ScheduleDensity(
            matches_last_7_days=matches_last,
            matches_next_7_days=matches_next,
            rest_days=rest_days,
            back_to_back=bool(last_game and (now - last_game) < timedelta(hours=30)),
            three_in_four=matches_last >= 3,
            notes=f"nhl: последняя игра {last_game.date().isoformat()}" if last_game else None,
        )

    async def get_injuries(self, team: str, **kwargs: Any) -> list[Any]:
        """НХЛ не отдаёт травмы в публичном API — возвращаем пусто (деградация).

        Травмы подтягиваются из LLM (по новостям) и MMA/новостных RSS — см. collector.
        """
        logger.debug("nhl: травмы по {} недоступны в публичном API", team)
        return []

    async def get_predicted_lineups(self, team: str, **kwargs: Any) -> Any:
        """НХЛ не публикует составы заранее — только «вероятный вратарь» (см. get_probable_goalie)."""
        return None

    # ------------------------------------------------------ вероятный вратарь
    async def _boxscore(self, game_id: int) -> dict[str, Any]:
        async def factory() -> dict[str, Any]:
            payload = await self.get_json(f"/gamecenter/{game_id}/boxscore")
            return payload if isinstance(payload, dict) else {}

        try:
            return await self._boxscore_cache.get_or_set(f"box:{game_id}", factory)
        except Exception as exc:
            logger.debug("nhl: boxscore {} недоступен ({})", game_id, exc)
            return {}

    @staticmethod
    def _starter_goalie(boxscore: dict[str, Any], side: str) -> str | None:
        """Вратарь с максимальным TOI в протоколе = стартовавший матч."""
        players = (((boxscore.get("playerByGameStats") or {}).get(side)) or {}).get("goalies") or []
        best_name: str | None = None
        best_time = -1
        for goalie in players:
            if not isinstance(goalie, dict):
                continue
            seconds = _int(goalie.get("toi")) or 0
            name = ((goalie.get("name") or {}).get("default")) if isinstance(goalie.get("name"), dict) else goalie.get("name")
            if name and seconds > best_time:
                best_name, best_time = str(name), seconds
        return best_name

    async def get_goalie_history(self, team: str, games: int = 5) -> list[dict[str, Any]]:
        """[{'date': datetime, 'goalie': 'Ilya Sorokin'} ...] — кто стартовал последние матчи."""
        abbrev = await self.resolve_team(team)
        if not abbrev:
            return []
        finished: list[tuple[datetime, dict[str, Any], bool]] = []
        for game in await self._club_schedule(abbrev):
            if not self._is_finished(game):
                continue
            moment = _parse_date(game.get("gameDate") or game.get("startTimeUTC"))
            if moment is None:
                continue
            _, _, is_home = self._game_result(game, abbrev)
            if is_home is None:
                continue
            finished.append((moment, game, is_home))
        finished.sort(key=lambda item: item[0], reverse=True)

        history: list[dict[str, Any]] = []
        for moment, game, is_home in finished[:games]:
            game_id = _int(game.get("id"))
            if not game_id:
                continue
            boxscore = await self._boxscore(game_id)
            goalie = self._starter_goalie(boxscore, "homeTeam" if is_home else "awayTeam")
            if goalie:
                history.append({"date": moment, "goalie": goalie, "is_home": is_home})
        return history

    async def get_probable_goalie(self, team: str, opponent: str | None = None) -> dict[str, Any] | None:
        """Эвристика «вероятный стартовый вратарь» команды на следующий матч.

        Логика: вратарь, который не играл в предыдущем матче команды, но провёл больше
        всего стартов (бэкап выходит в back-to-back). Это НЕ официальные данные NHL.
        """
        history = await self.get_goalie_history(team, games=6)
        if not history:
            return None
        last_game_date = history[0]["date"]
        last_goalie = history[0]["goalie"]
        counts: dict[str, int] = {}
        for entry in history:
            counts[entry["goalie"]] = counts.get(entry["goalie"], 0) + 1
        rest_days = (datetime.now(timezone.utc) - last_game_date).days
        # Если между матчами ≤1 день и есть явный второй вратарь — в NHL обычно играет он.
        if rest_days <= 1 and len(counts) >= 2:
            candidate = max((name for name in counts if name != last_goalie), key=lambda name: counts[name], default=last_goalie)
            reason = f"back-to-back (последний старт {last_game_date.date().isoformat()}), в серии стартовал {last_goalie}"
        else:
            candidate = max(counts, key=lambda name: counts[name])
            reason = f"больше всего стартов за последние {len(history)} матчей"
        return {
            "team": team,
            "goalie": candidate,
            "starts_last_games": counts.get(candidate, 0),
            "games_checked": len(history),
            "rest_days": rest_days,
            "reason": reason,
            "source": "nhl (эвристика по boxscore)",
        }

    # ------------------------------------------------- голы/броски для Пуассона
    async def get_team_goal_stats(self, team: str, n: int = 10) -> TeamXgStats | None:
        """Фолбэк-статистика для Пуассона: голы за/против (дома/в гостях) из расписания."""
        abbrev = await self.resolve_team(team)
        if not abbrev:
            return None
        home_for: list[int] = []
        home_against: list[int] = []
        away_for: list[int] = []
        away_against: list[int] = []
        finished: list[tuple[datetime, dict[str, Any]]] = []
        for game in await self._club_schedule(abbrev):
            if not self._is_finished(game):
                continue
            moment = _parse_date(game.get("gameDate") or game.get("startTimeUTC"))
            if moment is None:
                continue
            finished.append((moment, game))
        finished.sort(key=lambda item: item[0], reverse=True)

        for _, game in finished[:n]:
            goals_for, goals_against, is_home = self._game_result(game, abbrev)
            if goals_for is None or goals_against is None:
                continue
            if is_home:
                home_for.append(goals_for)
                home_against.append(goals_against)
            else:
                away_for.append(goals_for)
                away_against.append(goals_against)

        sample = len(home_for) + len(away_for)
        if sample == 0:
            return None

        def avg(values: list[int]) -> float | None:
            return round(sum(values) / len(values), 3) if values else None

        return TeamXgStats(
            team=team,
            matches=sample,
            xg_for_per_game=avg(home_for + away_for),
            xg_against_per_game=avg(home_against + away_against),
            xg_for_home=avg(home_for),
            xg_against_home=avg(home_against),
            xg_for_away=avg(away_for),
            xg_against_away=avg(away_against),
            source="nhl:goals(фолбэк вместо xG MoneyPuck)",
        )

    # ------------------------------------------------------------------ extras
    async def get_club_stats(self, team: str) -> dict[str, Any]:
        """Сезонная статистика клуба: /club-stats/{abbrev}/now (PP%, PK%, броски)."""
        abbrev = await self.resolve_team(team)
        if not abbrev:
            return {}
        try:
            payload = await self.get_json(f"/club-stats/{abbrev}/now")
        except Exception as exc:
            logger.debug("nhl: club-stats {} недоступно ({})", abbrev, exc)
            return {}
        return payload if isinstance(payload, dict) else {}

    async def get_todays_games(self, day: date | None = None) -> list[dict[str, Any]]:
        """Все матчи NHL на дату (для матчинга расписаний)."""
        target = (day or datetime.now(timezone.utc).date()).isoformat()
        try:
            payload = await self.get_json(f"/score/{target}")
        except Exception as exc:
            logger.warning("nhl: /score/{} недоступен ({})", target, exc)
            return []
        games = payload.get("games", []) if isinstance(payload, dict) else []
        return [game for game in games if isinstance(game, dict)]
