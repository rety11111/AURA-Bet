"""Liquipedia LPDB v3: матчи и tier турниров (нужен выданный Liquipedia API key).

v3 использует ресурсные маршруты /match и /tournament, а не /query.
Live-счёт/драфты берутся из OpenDota, Liquipedia — только календарь.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from loguru import logger

from app.config import settings
from app.sources.base import (
    CircuitBreakerOpen,
    EsportsLiveMatch,
    EsportsProvider,
    MapStats,
    SourceError,
    TTLCache,
)

WIKI_BY_SPORT = {"dota2": "dota2", "cs2": "counterstrike"}
# Соответствие числового tier Liquipedia → строке, которую понимает скринер.
TIER_LABELS = {"1": "S-Tier", "2": "A-Tier", "3": "B-Tier", "4": "C-Tier", "5": "D-Tier"}
NON_TIER_TYPES = ("qualifier", "monthly", "weekly", "showmatch", "show match", "charity", "unranked")


def _parse_datetime(raw: Any) -> datetime | None:
    if raw in (None, ""):
        return None
    if isinstance(raw, (int, float)):
        try:
            return datetime.fromtimestamp(float(raw), tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(raw).strip()
    if text.isdigit():
        return _parse_datetime(int(text))
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d", "%Y-%m-%d %H:%M"):
        try:
            parsed = datetime.strptime(text.replace("Z", "+0000"), fmt)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            continue
    logger.debug("liquipedia: не удалось разобрать дату {}", raw)
    return None


def deep_find_key(node: Any, key_fragment: str, depth: int = 6) -> list[Any]:
    """Рекурсивно ищет значения по «частичному» имени ключа в произвольном JSON.

    Нужно потому, что LPDB отдаёт вложенные структуры вида
    {"result":[{"type":"match2","result":[{...}]}]} и мы не хотим зависеть
    от точной глубины вложенности.
    """
    found: list[Any] = []
    fragment = key_fragment.lower()

    def walk(item: Any, level: int) -> None:
        if level > depth:
            return
        if isinstance(item, dict):
            for key, value in item.items():
                if fragment in str(key).lower():
                    found.append(value)
                walk(value, level + 1)
        elif isinstance(item, list):
            for value in item:
                walk(value, level + 1)

    walk(node, 0)
    return found


def _team_names_from_row(row: dict[str, Any]) -> list[str]:
    """Имена команд из строки match2/match2opponents (в разных схемах они лежат по-разному)."""
    names: list[str] = []
    for key in ("opponent1", "opponent2"):
        value = row.get(key)
        if isinstance(value, dict):
            name = value.get("name") or value.get("template") or value.get("team")
            if name:
                names.append(str(name))
        elif isinstance(value, str):
            names.append(value)
    for value in deep_find_key(row, "opponent", depth=3):
        items = value if isinstance(value, list) else [value]
        for item in items:
            if isinstance(item, dict):
                name = item.get("name") or item.get("template") or item.get("team")
                if name and str(name) not in names:
                    names.append(str(name))
    return names


def _score_for_row(row: dict[str, Any]) -> tuple[int | None, int | None]:
    """Счёт серии из строки матча."""
    goals = row.get("goals") or row.get("score") or row.get("scores")
    if isinstance(goals, dict):
        home = goals.get("h") if "h" in goals else goals.get("home")
        away = goals.get("a") if "a" in goals else goals.get("away")
        try:
            return (int(home), int(away)) if home not in (None, "") and away not in (None, "") else (None, None)
        except (TypeError, ValueError):
            return None, None
    for key in ("score1", "opponent1score", "results1"):
        if key in row:
            try:
                first = int(float(row[key]))
            except (TypeError, ValueError):
                first = None
            break
    else:
        first = None
    for key in ("score2", "opponent2score", "results2"):
        if key in row:
            try:
                second = int(float(row[key]))
            except (TypeError, ValueError):
                second = None
            break
    else:
        second = None
    scores = deep_find_key(row, "score", depth=3)
    if first is None and scores:
        value = scores[0]
        if isinstance(value, dict):
            try:
                first = int(float(value.get("h", value.get("home"))))
                second = int(float(value.get("a", value.get("away"))))
            except (TypeError, ValueError):
                pass
    return first, second


class LiquipediaProvider(EsportsProvider):
    """LPDB API v3: расписания, tier турниров, статистика по картам."""

    provider_name = "liquipedia"

    def __init__(self, api_url: str | None = None, user_agent: str | None = None, api_key: str | None = None) -> None:
        ua = (user_agent or settings.liquipedia_user_agent or "").strip()
        headers = {"Accept": "application/json"}
        if ua:
            headers["User-Agent"] = ua
        self._api_key = (api_key if api_key is not None else settings.liquipedia_api_key or "").strip()
        if self._api_key:
            headers["Authorization"] = f"Apikey {self._api_key}"
        base = (api_url if api_url is not None else settings.liquipedia_api_url).rstrip("/")
        # Старые Railway-переменные могли сохранить несуществующий маршрут /query.
        if base.endswith("/query"):
            base = base.removesuffix("/query")
            logger.warning("liquipedia: устаревший LIQUIPEDIA_API_URL /query; использую {}", base)
        super().__init__(source_name="liquipedia", base_url=base, headers=headers)
        self._cache = TTLCache(ttl_sec=settings.cache_ttl_liquipedia_sec)
        self._disabled_reason: str | None = None
        self._warned_missing_config = False

    @property
    def available(self) -> bool:
        if self._disabled_reason:
            return False
        if not self.base_url or not self._api_key or "example.com" in (self.headers.get("User-Agent") or ""):
            if not self._warned_missing_config:
                logger.warning("liquipedia: нужен реальный LIQUIPEDIA_USER_AGENT и выданный LIQUIPEDIA_API_KEY; источник отключён")
                self._warned_missing_config = True
            return False
        return True

    # ------------------------------------------------------------------- query
    async def query(
        self,
        wiki: str,
        resource: str,
        *,
        conditions: str | None = None,
        order: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Один запрос LPDB v3 к ресурсу; ошибку источника не пробрасываем в pipeline."""
        if not self.available:
            return []
        params: dict[str, Any] = {"wiki": wiki, "limit": limit, "offset": offset}
        if conditions:
            params["conditions"] = conditions
        if order:
            params["order"] = order
        try:
            payload = await self.get_json(f"/{resource}", params=params, polite=True)
        except CircuitBreakerOpen:
            # OPEN — ожидаемое состояние, не логируем на каждый тик и по каждому wiki.
            return []
        except SourceError as exc:
            message = str(exc)
            if any(f"HTTP {code} " in message for code in (401, 403, 404)):
                self._disabled_reason = message
                logger.error(
                    "liquipedia: постоянная ошибка API ({}); отключён до перезапуска. "
                    "Проверьте LIQUIPEDIA_API_URL/KEY и доступ к LPDB v3", message[:200],
                )
            else:
                logger.warning("liquipedia: запрос {} wiki={} не удался ({})", resource, wiki, message[:200])
            return []
        except Exception as exc:  # сеть/таймаут — источник просто недоступен
            logger.warning("liquipedia: сеть/таймаут при запросе {} wiki={} ({})", resource, wiki, exc)
            return []

        if not isinstance(payload, dict):
            logger.warning("liquipedia: неожиданный формат ответа {} wiki={}", resource, wiki)
            return []
        if payload.get("error"):
            logger.warning("liquipedia: ошибка API {} wiki={}: {}", resource, wiki, str(payload["error"])[:200])
            return []
        rows = payload.get("result")
        return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []

    # ------------------------------------------------------------ расписания
    async def get_upcoming_matches(self, sport: str, days: int = 3) -> list[EsportsLiveMatch]:
        """Запланированные матчи на ближайшие `days` дней (finished=0)."""
        wiki = WIKI_BY_SPORT.get(sport.lower())
        if not wiki:
            logger.debug("liquipedia: спорт '{}' не поддерживается (только dota2/cs2)", sport)
            return []
        now = datetime.now(UTC)
        horizon = now + timedelta(days=days)
        key = f"upcoming:{wiki}:{days}"

        async def factory() -> list[EsportsLiveMatch]:
            rows = await self.query(
                wiki, "match",
                conditions=f"[[finished::0]] AND [[date::>{now:%Y-%m-%d %H:%M:%S}]] "
                           f"AND [[date::<{horizon:%Y-%m-%d %H:%M:%S}]]",
                order="date ASC", limit=60,
            )
            matches: list[EsportsLiveMatch] = []
            for row in rows:
                moment = _parse_datetime(row.get("date") or row.get("match2.date"))
                if moment is None or not (now - timedelta(hours=6) <= moment <= horizon):
                    continue
                names = _team_names_from_row(row)
                if len(names) < 2:
                    continue
                tournament = str(row.get("tournament") or row.get("pagename") or "Unknown")
                tier_raw = str(row.get("liquipediatier") or "")
                tier = TIER_LABELS.get(tier_raw) or (row.get("liquipediatiertype") if isinstance(row.get("liquipediatiertype"), str) else None)
                matches.append(
                    EsportsLiveMatch(
                        ext_id=str(row.get("match2id") or f"{wiki}:{names[0]}:{names[1]}:{moment.isoformat()}"),
                        sport=sport.lower(),
                        league=tournament,
                        league_tier=tier,
                        team_a=names[0],
                        team_b=names[1],
                        source="liquipedia",
                        match_time=moment,
                        series_score=None,
                        extra={"bestof": row.get("bestof"), "raw_source": "match2" if row.get("match2id") else "match2opponents"},
                    )
                )
            logger.info("liquipedia: {} → {} матчей на ближайшие {} дн.", wiki, len(matches), days)
            return matches

        return await self._cache.get_or_set(key, factory, ttl_sec=settings.cache_ttl_liquipedia_sec)

    async def get_live_matches(self, sport: str) -> list[EsportsLiveMatch]:
        """Liquipedia не является live-источником.

        Возвращаем матчи, которые начались не позднее 4 часов назад и ещё не помечены
        завершёнными — это максимум, что даёт LPDB. Реальный лайв (драфт, счёт, экономика)
        берём из OpenDota, CS2-лайв — из Winline/BetBoom live odds.
        """
        wiki = WIKI_BY_SPORT.get(sport.lower())
        if not wiki:
            return []
        now = datetime.now(UTC)

        async def factory() -> list[EsportsLiveMatch]:
            rows = await self.query(
                wiki, "match",
                conditions=f"[[finished::0]] AND [[date::>{now - timedelta(hours=6):%Y-%m-%d %H:%M:%S}]]",
                order="date ASC", limit=30,
            )
            matches: list[EsportsLiveMatch] = []
            for row in rows:
                moment = _parse_datetime(row.get("date") or row.get("match2.date"))
                if moment is None or not (now - timedelta(hours=4) <= moment <= now + timedelta(minutes=15)):
                    continue
                names = _team_names_from_row(row)
                if len(names) < 2:
                    continue
                first, second = _score_for_row(row)
                matches.append(
                    EsportsLiveMatch(
                        ext_id=str(row.get("match2id") or f"{wiki}:{names[0]}:{names[1]}"),
                        sport=sport.lower(),
                        league=str(row.get("tournament") or "Unknown"),
                        league_tier=TIER_LABELS.get(str(row.get("liquipediatier") or "")),
                        team_a=names[0],
                        team_b=names[1],
                        source="liquipedia",
                        match_time=moment,
                        series_score=(first, second) if first is not None and second is not None else None,
                        extra={"live_source": "liquipedia (только факт начала матча, без счёта по картам)"},
                    )
                )
            return matches

        return await self._cache.get_or_set(f"live:{wiki}", factory, ttl_sec=settings.cache_ttl_esports_sec)

    async def get_draft(self, match_id: str) -> tuple[list[str], list[str], list[str], list[str]]:
        """Liquipedia драфты не отдаёт — драфт берётся из OpenDota (Dota 2).

        Возвращаем пустые списки, чтобы вызывающий код не падал и продолжил работу
        без драфт-контекста (см. live/esports_worker.py: драфт — обязательное условие
        для сигнала по Dota, поэтому без OpenDota лайв-сигналов по Dota не будет).
        """
        logger.debug("liquipedia: get_draft({}) — драфт доступен только через OpenDota", match_id)
        return [], [], [], []

    # -------------------------------------------------------- статистика карт
    async def get_map_stats(self, team: str, map_name: str) -> MapStats | None:
        """Винрейт команды по конкретной карте (CS2: Nuke/Mirage/..., Dota: карта = матч).

        Реализация: /match возвращает вложенные match2opponents и match2games;
        локально выбираем матчи команды и считаем winrate по раундам.
        Поле `winner` в match2games означает индекс победившего оппонента (1/2).
        """
        team_key = (team or "").strip().lower()
        map_key = (map_name or "").strip().lower()
        if not team_key or not map_key:
            return None

        async def factory() -> MapStats | None:
            for wiki in WIKI_BY_SPORT.values():
                rows = await self.query(wiki, "match", conditions="[[finished::1]]", order="date DESC", limit=200)
                if not rows:
                    continue
                wins = losses = rounds_won = rounds_lost = 0
                for row in rows:
                    opponents = _team_names_from_row(row)
                    try:
                        team_index = next(index for index, name in enumerate(opponents, start=1) if team_key == name.lower())
                    except StopIteration:
                        continue
                    for game in row.get("match2games") or []:
                        if not isinstance(game, dict):
                            continue
                        game_map = str(game.get("map") or "").strip().lower()
                        if not game_map or (map_key not in game_map and game_map not in map_key):
                            continue
                        winner = str(game.get("winner") or "").strip()
                        first = game.get("score1")
                        second = game.get("score2")
                        try:
                            score_a = int(float(first)) if first not in (None, "") else None
                            score_b = int(float(second)) if second not in (None, "") else None
                        except (TypeError, ValueError):
                            score_a = score_b = None
                        if score_a is not None and score_b is not None:
                            team_score, opp_score = (score_a, score_b) if team_index == 1 else (score_b, score_a)
                            rounds_won += team_score
                            rounds_lost += opp_score
                            if team_score > opp_score:
                                wins += 1
                            elif team_score < opp_score:
                                losses += 1
                        if winner and winner == str(team_index):
                            wins += 1 if score_a is None else 0
                        elif winner and winner in ("1", "2") and winner != str(team_index):
                            losses += 1 if score_a is None else 0
                games = wins + losses
                if games == 0:
                    continue
                return MapStats(
                    team=team,
                    map_name=map_name,
                    matches=games,
                    win_rate=round(wins / games, 4),
                    round_win_rate=(
                        round(rounds_won / (rounds_won + rounds_lost), 4) if (rounds_won + rounds_lost) else None
                    ),
                    source=f"liquipedia:{wiki}:match2games",
                )
            logger.info(
                "liquipedia: статистика по карте '{}' для '{}' не найдена "
                "(проверьте названия полей таблицы match2games своим ключом)",
                map_name, team,
            )
            return None

        return await self._cache.get_or_set(f"map:{team_key}:{map_key}", factory, ttl_sec=settings.cache_ttl_liquipedia_sec)

    # ------------------------------------------------------------ tier турнира
    async def get_tournament_tier(self, tournament: str) -> str | None:
        """Tier турнира: 'S-Tier' / 'A-Tier' / ... либо тип ('Qualifier' и т.п.)."""
        name = (tournament or "").strip()
        if not name:
            return None

        async def factory() -> str | None:
            for wiki in WIKI_BY_SPORT.values():
                rows = await self.query(
                    wiki, "tournament", conditions=f"[[name::{name}]]", limit=5,
                )
                for row in rows:
                    tiertype = str(row.get("liquipediatiertype") or "").strip()
                    if str(row.get("name") or "").casefold() != name.casefold():
                        continue
                    if tiertype and any(marker in tiertype.lower() for marker in NON_TIER_TYPES):
                        return tiertype
                    tier = str(row.get("liquipediatier") or "").strip()
                    if tier in TIER_LABELS:
                        return TIER_LABELS[tier]
                    if tier.lower().startswith("s") or "tier 1" in tier.lower():
                        return "S-Tier"
            return None

        return await self._cache.get_or_set(f"tier:{name.lower()}", factory, ttl_sec=settings.cache_ttl_liquipedia_sec)
