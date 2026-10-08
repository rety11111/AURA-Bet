"""Liquipedia — киберспортивные расписания, tier турниров и статистика по картам.

ВАЖНО (честно — обязательно прочитайте перед продакшеном):
  * У Liquipedia НЕТ стабильного публичного REST API. Используется LPDB API v3
    (`settings.liquipedia_api_url`), который требует:
      - корректный User-Agent БЕЗ него запросы блокируются. Формат обязателен:
        «BetSignalsBot/1.0 (contact: your@email)» — задаётся в .env как
        LIQUIPEDIA_USER_AGENT (см. .env.example). Наш дефолт — только заглушка.
      - часто ещё и Api-Key: LIQUIPEDIA_API_KEY. Если ключа нет, а API его требует,
        мы получим 401/403 — провайдер честно пишет об этом в лог и возвращает пусто,
        сервис продолжает работать на остальных источниках.
  * Точные имена таблиц/полей LPDB (`match2`, `match2opponents`, `match2games`,
    `tournament`) и синтаксис параметров (JSON-массивы для tables/fields/conditions)
    МОГУТ отличаться в вашей версии API. Парсер ниже устойчив к форме ответа
    (ищет нужные ключи рекурсивно), а scripts/check_sources.py проверяет вызов
    на реальном ключе и печатает сырой ответ.
  * Liquipedia НЕ отдаёт live-счёт матчей — это не источник лайва. Лайв-счёт,
    драфты и экономику даёт OpenDota (Dota 2), см. sources/opendota.py.
    Поэтому `get_live_matches` здесь строит «недавно начавшиеся» матчи по дате.

Ограничения LPDB: не более ~1 запроса в 2 секунды с одного IP (мы соблюдаем
вежливые паузы) и не более 60 запросов в час на ключ по умолчанию.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from loguru import logger

from app.config import settings
from app.sources.base import (
    BaseHttpClient,
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


def flatten_rows(payload: Any, wanted: tuple[str, ...] = ("result",)) -> list[dict[str, Any]]:
    """Достаёт список записей из ответа LPDB (структура может быть разной вложенности)."""
    rows: list[dict[str, Any]] = []
    for value in deep_find_key(payload, "result"):
        if isinstance(value, list):
            rows.extend(item for item in value if isinstance(item, dict))
        elif isinstance(value, dict):
            rows.append(value)
    if rows:
        return rows
    # Фолбэк: сам ответ уже список
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    for key in wanted:
        value = payload.get(key) if isinstance(payload, dict) else None
        if isinstance(value, list):
            rows.extend(item for item in value if isinstance(item, dict))
    return rows


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
        key = (api_key if api_key is not None else settings.liquipedia_api_key or "").strip()
        if key:
            # LPDB v3 использует заголовок Api-Key (некоторые инсталляции — Authorization).
            headers["Api-Key"] = key
            headers["Authorization"] = f"Api-Key {key}"
        super().__init__(source_name="liquipedia", base_url=api_url or settings.liquipedia_api_url, headers=headers)
        self._cache = TTLCache(ttl_sec=settings.cache_ttl_liquipedia_sec)

    @property
    def available(self) -> bool:
        if not self.base_url:
            return False
        if "example.com" in (self.headers.get("User-Agent") or ""):
            logger.warning(
                "liquipedia: User-Agent не настроен (LIQUIPEDIA_USER_AGENT). "
                "Liquipedia блокирует такие запросы — провайдер отключён до настройки .env"
            )
            return False
        return True

    # ------------------------------------------------------------------- query
    async def query(
        self,
        wiki: str,
        tables: list[str],
        fields: list[str],
        conditions: list[list[Any]] | None = None,
        order: list[str] | None = None,
        limit: int = 50,
        offset: int = 0,
        groupby: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Низкоуровневый запрос к LPDB v3.

        Параметры tables/fields/conditions/order/groupby передаются как JSON-массивы
        (синтаксис LPDB API v3). Если API вернёт 400 из-за формата — смотрите
        response.text в логе: там будет точная причина.
        """
        params: dict[str, Any] = {
            "wiki": wiki,
            "tables": json.dumps(tables, ensure_ascii=False),
            "fields": json.dumps(fields, ensure_ascii=False),
            "limit": str(limit),
            "offset": str(offset),
        }
        if conditions:
            params["conditions"] = json.dumps(conditions, ensure_ascii=False)
        if order:
            params["order"] = json.dumps(order, ensure_ascii=False)
        if groupby:
            params["groupby"] = json.dumps(groupby, ensure_ascii=False)

        try:
            payload = await self.get_json("", params=params, polite=True)
        except SourceError as exc:
            message = str(exc)
            if "401" in message or "403" in message:
                logger.error(
                    "liquipedia: доступ запрещён ({}). Проверьте LIQUIPEDIA_USER_AGENT и, "
                    "при необходимости, LIQUIPEDIA_API_KEY. Сырой ответ: {}",
                    wiki, message[:300],
                )
            else:
                logger.warning("liquipedia: запрос не удался для wiki={} ({})", wiki, message[:300])
            return []
        except Exception as exc:  # сеть/таймаут — источник просто недоступен
            logger.warning("liquipedia: сеть/таймаут при запросе wiki={} ({})", wiki, exc)
            return []

        if isinstance(payload, dict) and payload.get("error"):
            logger.warning("liquipedia: API вернул ошибку для wiki={}: {}", wiki, str(payload["error"])[:300])
        return flatten_rows(payload)

    # ------------------------------------------------------------ расписания
    async def get_upcoming_matches(self, sport: str, days: int = 3) -> list[EsportsLiveMatch]:
        """Запланированные матчи на ближайшие `days` дней (finished=0)."""
        wiki = WIKI_BY_SPORT.get(sport.lower())
        if not wiki:
            logger.debug("liquipedia: спорт '{}' не поддерживается (только dota2/cs2)", sport)
            return []
        now = datetime.now(timezone.utc)
        horizon = now + timedelta(days=days)
        key = f"upcoming:{wiki}:{days}"

        async def factory() -> list[EsportsLiveMatch]:
            rows = await self.query(
                wiki=wiki,
                tables=["match2", "match2opponents"],
                fields=[
                    "match2.match2id",
                    "match2.date",
                    "match2.finished",
                    "match2.bestof",
                    "match2.tournament",
                    "match2.liquipediatier",
                    "match2.liquipediatiertype",
                    "match2.pagename",
                    "match2opponents.name",
                    "match2opponents.score",
                    "match2opponents.match2id",
                ],
                conditions=[["match2.finished", "=", "0"]],
                order=["match2.date ASC"],
                limit=60,
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
        now = datetime.now(timezone.utc)

        async def factory() -> list[EsportsLiveMatch]:
            rows = await self.query(
                wiki=wiki,
                tables=["match2", "match2opponents"],
                fields=["match2.match2id", "match2.date", "match2.finished", "match2.bestof", "match2.tournament", "match2.liquipediatier"],
                conditions=[["match2.finished", "=", "0"], ["match2.date", ">", (now - timedelta(hours=6)).strftime("%Y-%m-%d %H:%M:%S")]],
                order=["match2.date ASC"],
                limit=30,
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

        Реализация: тянем матчи команды из match2/match2opponents/match2games,
        оставляем игры, сыгранные на нужной карте, и считаем winrate по раундам.
        Поле `winner` в match2games означает индекс победившего оппонента (1/2).
        """
        team_key = (team or "").strip().lower()
        map_key = (map_name or "").strip().lower()
        if not team_key or not map_key:
            return None

        async def factory() -> MapStats | None:
            for sport, wiki in WIKI_BY_SPORT.items():
                rows = await self.query(
                    wiki=wiki,
                    tables=["match2games", "match2opponents"],
                    fields=[
                        "match2games.map",
                        "match2games.score1",
                        "match2games.score2",
                        "match2games.winner",
                        "match2games.match2id",
                        "match2opponents.name",
                        "match2opponents.match2id",
                    ],
                    conditions=[["match2opponents.name", "=", team]],
                    limit=200,
                )
                if not rows:
                    continue
                row_groups: dict[str, list[dict[str, Any]]] = {}
                for row in rows:
                    match_id = str(row.get("match2id") or row.get("match2opponents.match2id") or "")
                    if not match_id:
                        continue
                    row_groups.setdefault(match_id, []).append(row)

                wins = losses = rounds_won = rounds_lost = 0
                for _, group in row_groups.items():
                    opponents = _team_names_from_row({"opponent": group})
                    try:
                        team_index = next(index for index, name in enumerate(opponents, start=1) if team_key in name.lower())
                    except StopIteration:
                        team_index = 1
                    for row in group:
                        game_map = str(row.get("map") or "").strip().lower()
                        if map_key not in game_map and game_map not in map_key:
                            continue
                        winner = str(row.get("winner") or "").strip()
                        first = row.get("score1")
                        second = row.get("score2")
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
                    wiki=wiki,
                    tables=["tournament"],
                    fields=["tournament.name", "tournament.liquipediatier", "tournament.liquipediatiertype"],
                    conditions=[["tournament.name", "=", name]],
                    limit=5,
                )
                for row in rows:
                    tiertype = str(row.get("liquipediatiertype") or "").strip()
                    if tiertype and any(marker in tiertype.lower() for marker in NON_TIER_TYPES):
                        return tiertype
                    tier = str(row.get("liquipediatier") or "").strip()
                    if tier in TIER_LABELS:
                        return TIER_LABELS[tier]
                    if tier.lower().startswith("s") or "tier 1" in tier.lower():
                        return "S-Tier"
                # Точное совпадение не нашлось — попробуем по вхождению
                rows = await self.query(
                    wiki=wiki,
                    tables=["tournament"],
                    fields=["tournament.name", "tournament.liquipediatier", "tournament.liquipediatiertype"],
                    conditions=[["tournament.name", "LIKE", f"%{name}%"]],
                    limit=5,
                )
                for row in rows:
                    tier = str(row.get("liquipediatier") or "").strip()
                    if tier in TIER_LABELS:
                        return TIER_LABELS[tier]
            return None

        return await self._cache.get_or_set(f"tier:{name.lower()}", factory, ttl_sec=settings.cache_ttl_liquipedia_sec)
