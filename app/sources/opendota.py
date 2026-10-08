"""OpenDota — Dota 2: лайв-матчи, драфт, детали матчей, история (бесплатно, без ключа).

Эндпоинты (официальная документация https://docs.opendota.com):
  GET /live            — матчи, которые прямо сейчас идут
  GET /matches/{id}    — детали матча (пост-факт: пики/баны, networth, deaths, last_hits)
  GET /heroes          — справочник героев (id → localized_name)
  GET /proMatches      — последние профессиональные матчи (нужны для tier-детекции и Elo)
  GET /leagues         — справочник лиг

⚠️ ЧЕСТНО ПРО ЛАЙВ-ДАННЫЕ ⚠️
В ТЗ сказано, что `/api/live` отдаёт «драфт, счёт, networth/XP/CS по игрокам, смерти».
Практика: `/api/live` гарантированно содержит `match_id`, `game_time`, `spectators`,
`players` (по 5 на команду — с `account_id` и `hero_id`) и признаки лобби. Часть
метрик (net_worth/xp/last_hits/deaths) появляется в лайв-объекте не у всех матчей —
поэтому парсер берёт их «если есть» и помечает в extra `networth_available: false`,
когда их нет. Драфт из `/live` мы восстанавливаем по `hero_id` игроков (первые 5 —
Radiant, вторые 5 — Dire): это и есть список пиков в лайве. Полные данные (пики/баны,
networth по минутам) доступны в `/matches/{id}` уже после игры — метод
`get_match_details()` используется для Elo-обновления и обучения.

Такой подход даёт рабочие «окна» без платных источников:
  Окно 1 (после драфта): пики против лайв-кэфа.
  Окно 2 (8–12 минуты): счёт/убийства/золото — если OpenDota отдаёт networth, используем;
  если нет, сравниваем счёт/время с кэфом по более грубым сигналам.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from loguru import logger

from app.config import settings
from app.sources.base import (
    EsportsLiveMatch,
    EsportsProvider,
    LivePlayerStat,
    MapStats,
    SourceError,
    TTLCache,
    safe_int,
    safe_float,
)


class OpenDotaProvider(EsportsProvider):
    provider_name = "opendota"

    def __init__(self, base_url: str | None = None, **kwargs: Any) -> None:
        super().__init__(source_name=self.provider_name, **kwargs)
        self.base_url = (base_url or settings.opendota_api_base).rstrip("/")
        self._cache = TTLCache(ttl_sec=settings.cache_ttl_esports_sec, maxsize=512)
        self._heroes: dict[int, str] = {}

    @property
    def available(self) -> bool:
        return bool(self.base_url)

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        try:
            return await self.get_json(f"{self.base_url}{path}", params=params or {})
        except SourceError as exc:
            logger.warning("{}: {} недоступен ({})", self.provider_name, path, exc)
            return None

    # ---------------------------------------------------------------- heroes
    async def hero_names(self) -> dict[int, str]:
        if self._heroes:
            return self._heroes

        payload = await self._cache.get_or_set("heroes", lambda: self._get("/heroes"), ttl_sec=24 * 3600)
        heroes: dict[int, str] = {}
        for hero in payload or []:
            if not isinstance(hero, dict):
                continue
            hero_id = safe_int(hero.get("id"))
            if hero_id is None:
                continue
            heroes[hero_id] = str(hero.get("localized_name") or hero.get("name") or hero_id)
        self._heroes = heroes
        return heroes

    def _hero(self, hero_id: Any) -> str:
        hid = safe_int(hero_id)
        if hid is None:
            return "unknown"
        return self._heroes.get(hid, f"hero_{hid}")

    # ------------------------------------------------------------------ live
    async def get_live_matches(self, sport: str = "dota2") -> list[EsportsLiveMatch]:
        if sport not in ("dota2", "dota"):
            raise SourceError(f"{self.provider_name}: поддерживает только Dota 2")
        payload = await self._get("/live")
        if payload is None:
            return []
        await self.hero_names()  # прогреваем справочник, чтобы имена героев были человекочитаемыми

        live: list[EsportsLiveMatch] = []
        for game in payload:
            if not isinstance(game, dict):
                continue
            match_id = game.get("match_id")
            if match_id is None:
                continue
            players_raw = game.get("players") or []
            radiant_players, dire_players = [], []
            for index, player in enumerate(players_raw):
                if not isinstance(player, dict):
                    continue
                stat = LivePlayerStat(
                    account_id=safe_int(player.get("account_id")),
                    name=str(player.get("name") or "") or None,
                    hero=self._hero(player.get("hero_id")),
                    net_worth=safe_int(player.get("net_worth") or player.get("networth") or player.get("gold")),
                    xp=safe_int(player.get("xp")),
                    last_hits=safe_int(player.get("last_hits") or player.get("lh")),
                    deaths=safe_int(player.get("deaths")),
                    level=safe_int(player.get("level")),
                )
                (radiant_players if index < 5 else dire_players).append(stat)

            net_worth_a = _sum_optional(radiant_players, "net_worth")
            net_worth_b = _sum_optional(dire_players, "net_worth")
            xp_a = _sum_optional(radiant_players, "xp")
            xp_b = _sum_optional(dire_players, "xp")
            game_time = safe_int(game.get("game_time"))

            live.append(
                EsportsLiveMatch(
                    ext_id=str(match_id),
                    sport="dota2",
                    league=str(game.get("league_name") or game.get("league_id") or "unknown"),
                    league_tier=str(game.get("league_tier") or "") or None,
                    team_a=str(game.get("radiant_name") or game.get("radiant_team_name") or "Radiant"),
                    team_b=str(game.get("dire_name") or game.get("dire_team_name") or "Dire"),
                    source=self.provider_name,
                    match_time=_parse_ts(game.get("activate_time")),
                    game_time_sec=game_time,
                    game_minute=(game_time // 60) if game_time is not None else None,
                    draft_a=[p.hero for p in radiant_players if p.hero],
                    draft_b=[p.hero for p in dire_players if p.hero],
                    players_a=radiant_players,
                    players_b=dire_players,
                    net_worth_a=net_worth_a,
                    net_worth_b=net_worth_b,
                    xp_a=xp_a,
                    xp_b=xp_b,
                    is_draft_complete=len(radiant_players) == 5 and len(dire_players) == 5,
                    stage=(
                        f"драфт завершён, минута {game_time // 60}" if game_time is not None and game_time < 600
                        else (f"минута {game_time // 60}" if game_time is not None else "лайв")
                    ),
                    extra={
                        "lobby_id": game.get("lobby_id"),
                        "spectators": game.get("spectators"),
                        "delay": game.get("delay"),
                        "networth_available": net_worth_a is not None and net_worth_b is not None,
                        "radiant_win": game.get("radiant_win"),
                    },
                )
            )
        logger.info("{}: лайв-матчей Dota 2 → {}", self.provider_name, len(live))
        return live

    async def get_draft(self, match_id: str) -> tuple[list[str], list[str], list[str], list[str]]:
        """Драфт конкретного матча: (пики Radiant, пики Dire, баны Radiant, баны Dire).

        В лайве пики берутся из /live (по hero_id игроков), а баны — только из
        /matches/{id} (после матча). Метод отдаёт то, что реально доступно.
        """
        details = await self.get_match_details(match_id)
        if details:
            picks_bans = details.get("picks_bans") or []
            picks_a, picks_b, bans_a, bans_b = [], [], [], []
            for entry in picks_bans:
                if not isinstance(entry, dict):
                    continue
                hero = self._hero(entry.get("hero_id"))
                team = safe_int(entry.get("team"))  # 0 = radiant, 1 = dire
                target_picks = picks_a if team == 0 else picks_b
                target_bans = bans_a if team == 0 else bans_b
                (target_picks if entry.get("is_pick") else target_bans).append(hero)
            if picks_a or picks_b:
                return picks_a, picks_b, bans_a, bans_b
        # Фоллбэк: матч ещё идёт — берём пики из /live
        for match in await self.get_live_matches("dota2"):
            if match.ext_id == str(match_id):
                return match.draft_a, match.draft_b, match.draft_bans_a, match.draft_bans_b
        return [], [], [], []

    async def get_map_stats(self, team: str, map_name: str) -> MapStats | None:
        """Для Dota 2 понятие «карта» отсутствует (одна карта = матч).

        Честно возвращаем None: карты используются только в CS2 (Liquipedia/Elo).
        """
        return None

    async def get_tournament_tier(self, tournament: str) -> str | None:
        """Грубая оценка уровня турнира по названию (TI/мейджоры → tier-1)."""
        low = (tournament or "").lower()
        if any(k in low for k in ("the international", "ti 2", "ti1", "ti 1")):
            return "S-Tier"
        if any(k in low for k in ("major", "majors", "esl one", "pgl", "riyadh masters", "dreamleague")):
            return "Tier 1"
        return None

    # ------------------------------------------------------ match details/hist
    async def get_match_details(self, match_id: str) -> dict[str, Any] | None:
        """Детали матча (в т.ч. пост-факт: драфт, networth, deaths, last_hits)."""
        payload = await self._get(f"/matches/{match_id}")
        return payload if isinstance(payload, dict) else None

    async def get_pro_matches(self, limit: int = 100) -> list[dict[str, Any]]:
        payload = await self._cache.get_or_set(
            f"proMatches:{limit}", lambda: self._get("/proMatches"), ttl_sec=10 * 60
        )
        if not isinstance(payload, list):
            return []
        return payload[:limit]

    async def get_finished_matches_for_elo(self, hours_back: int = 6) -> list[dict[str, Any]]:
        """Завершённые профессиональные матчи за последние N часов — кормят Elo (sport=dota2)."""
        now = datetime.now(timezone.utc)
        out: list[dict[str, Any]] = []
        for match in await self.get_pro_matches(limit=100):
            start = _parse_ts(match.get("start_time"))
            if start is None:
                continue
            if 0 <= (now - start).total_seconds() <= hours_back * 3600:
                out.append(match)
        return out


def _sum_optional(players: list[LivePlayerStat], attribute: str) -> int | None:
    values = [getattr(player, attribute) for player in players if getattr(player, attribute) is not None]
    if not values:
        return None
    return int(sum(values))


def _parse_ts(value: Any) -> datetime | None:
    ts = safe_float(value)
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
