"""TheOddsApiProvider — fallback №2 по коэффициентам (free tier).

ВАЖНО ПРО ЛИМИТЫ: у the-odds-api.com free-план — 500 запросов в месяц.
Поэтому:
  * провайдер по умолчанию ВЫКЛЮЧЕН (THE_ODDS_API_ENABLED=false) и НЕ используется
    для регулярного поллинга;
  * все ответы кэшируются (TTL 15 минут для линий, 6 часов для списка спортов);
  * перед каждым запросом проверяется внутренний счётчик (THE_ODDS_API_MONTHLY_LIMIT,
    дефолт 450 — держим запас к 500). Счётчик живёт в памяти процесса: после рестарта
    он обнуляется, но лимит 450 всё равно даёт небольшой запас. Актуальный расход
    всегда виден в заголовках x-requests-remaining / x-requests-used — они пишутся в лог.

Документация API: https://the-odds-api.com/liveapi/guides/v4/
Спорт-ключи (soccer, icehockey_nhl, basketball_nba, ...) — официальные значения
API; полный список: GET /v4/sports/?apiKey=... (см. scripts/check_sources.py).
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from loguru import logger

from app.config import THE_ODDS_API_SPORT_KEYS, settings
from app.sources.base import Match, Odd, OddsProvider, SourceError, TTLCache, safe_float

_MARKET_MAP = {
    "h2h": "1x2",
    "totals": "totals",
    "spreads": "handicap",
    "double_chance": "double_chance",
}


class TheOddsApiProvider(OddsProvider):
    provider_name = "theoddsapi"

    def __init__(self, api_key: str | None = None, base_url: str | None = None, **kwargs: Any) -> None:
        super().__init__(source_name=self.provider_name, **kwargs)
        self.api_key = api_key if api_key is not None else settings.the_odds_api_key
        self.base_url = (base_url if base_url is not None else settings.the_odds_api_base).rstrip("/")
        self._cache = TTLCache(ttl_sec=15 * 60, maxsize=256)
        self._requests_used = 0
        self._events_by_id: dict[str, str] = {}  # event_id → sport key
        self._log_usage_headers: dict[str, str] = {}

    @property
    def available(self) -> bool:
        return bool(self.api_key) and settings.the_odds_api_enabled

    # ------------------------------------------------------------------ quota
    def _can_spend(self, n: int = 1) -> bool:
        if self._requests_used + n > settings.the_odds_api_monthly_limit:
            logger.warning(
                "{}: месячный лимит {} запросов исчерпан (использовано {}) — пропускаем вызов",
                self.provider_name, settings.the_odds_api_monthly_limit, self._requests_used,
            )
            return False
        return True

    async def _get(self, path: str, params: dict[str, Any]) -> Any:
        if not self.available:
            raise SourceError(f"{self.provider_name}: недоступен (нет ключа или THE_ODDS_API_ENABLED=false)")
        if not self._can_spend():
            raise SourceError(f"{self.provider_name}: исчерпан месячный лимит бесплатного тарифа")
        params = {**params, "apiKey": self.api_key}
        # httpx возвращает заголовки после запроса — читаем их в request/response
        response = await self.request("GET", f"{self.base_url}{path}", params=params)
        self._requests_used += 1
        for header in ("x-requests-remaining", "x-requests-used", "x-requests-last"):
            value = response.headers.get(header)
            if value is not None:
                self._log_usage_headers[header] = value
        logger.info(
            "{}: GET {} (использовано {} из {}; осталось: {})",
            self.provider_name, path, self._requests_used,
            settings.the_odds_api_monthly_limit, self._log_usage_headers.get("x-requests-remaining", "?"),
        )
        try:
            return response.json()
        except ValueError as exc:
            raise SourceError(f"{self.provider_name}: ответ не JSON") from exc

    def quota_info(self) -> dict[str, Any]:
        return {
            "requests_used_local": self._requests_used,
            "monthly_limit": settings.the_odds_api_monthly_limit,
            "headers": dict(self._log_usage_headers),
        }

    # ------------------------------------------------------------------ match
    async def get_upcoming(self, sport: str, day: date) -> list[Match]:
        sport_key = THE_ODDS_API_SPORT_KEYS.get(sport, sport)
        cache_key = f"upcoming:{sport_key}:{day.isoformat()}"

        async def factory() -> list[Match]:
            payload = await self._get(
                f"/sports/{sport_key}/odds",
                {
                    "regions": "eu",
                    "markets": "h2h,totals",
                    "oddsFormat": "decimal",
                    "dateFormat": "iso",
                },
            )
            if not isinstance(payload, list):
                raise SourceError(f"{self.provider_name}: неожиданный формат ответа ({type(payload).__name__})")
            matches: list[Match] = []
            for event in payload:
                if not isinstance(event, dict):
                    continue
                starts_at = event.get("commence_time")
                try:
                    start_dt = datetime.fromisoformat(str(starts_at).replace("Z", "+00:00")).astimezone(timezone.utc)
                except (TypeError, ValueError):
                    continue
                if start_dt.date() not in (day, day.replace(day=day.day)) and start_dt.date() != day:
                    # The Odds API отдаёт всё, что знает; фильтруем по дате локально.
                    if start_dt.date() != day:
                        continue
                event_id = str(event.get("id"))
                self._events_by_id[event_id] = sport_key
                matches.append(
                    Match(
                        ext_id=event_id,
                        sport=sport,
                        league=str(event.get("sport_title") or sport_key),
                        home_team=str(event.get("home_team") or ""),
                        away_team=str(event.get("away_team") or ""),
                        starts_at=start_dt,
                        source=self.provider_name,
                        extra={"bookmakers": len(event.get("bookmakers") or [])},
                    )
                )
            logger.info("{}: {} событий по ключу {}", self.provider_name, len(matches), sport_key)
            return matches

        try:
            return await self._cache.get_or_set(cache_key, factory)
        except SourceError:
            raise
        except Exception as exc:  # noqa: BLE001 — источник не должен ронять систему
            raise SourceError(f"{self.provider_name}: ошибка получения событий ({exc})") from exc

    # ------------------------------------------------------------------- odds
    def _parse_bookmakers(self, event: dict[str, Any]) -> list[Odd]:
        odds: list[Odd] = []
        for bookmaker in event.get("bookmakers") or []:
            if not isinstance(bookmaker, dict):
                continue
            book_title = str(bookmaker.get("title") or bookmaker.get("key") or "book")
            for market in bookmaker.get("markets") or []:
                if not isinstance(market, dict):
                    continue
                market_type = _MARKET_MAP.get(str(market.get("key")))
                if market_type is None:
                    continue
                for outcome in market.get("outcomes") or []:
                    if not isinstance(outcome, dict):
                        continue
                    price = safe_float(outcome.get("price"))
                    if not price or price <= 1.0:
                        continue
                    name = str(outcome.get("name") or "")
                    point = safe_float(outcome.get("point"))
                    selection = self._selection_for(market_type, name, event, point)
                    if selection is None:
                        continue
                    line = point if market_type in ("totals", "handicap") else None
                    odds.append(
                        Odd(market=market_type, selection=selection, line=line, price=price, source=self.provider_name)
                    )
        return odds

    @staticmethod
    def _selection_for(market_type: str, name: str, event: dict[str, Any], point: float | None) -> str | None:
        low = name.strip().lower()
        home = str(event.get("home_team") or "").strip().lower()
        away = str(event.get("away_team") or "").strip().lower()
        if market_type == "totals":
            if low in ("over", "больше"):
                return "over"
            if low in ("under", "меньше"):
                return "under"
            return None
        if market_type == "double_chance":
            return {"1x": "1x", "12": "12", "x2": "x2", "home or draw": "1x", "draw or away": "x2",
                    "home or away": "12"}.get(low)
        if low == "draw" or low == "ничья":
            return "draw"
        if home and (low == home or low in home):
            return "home_handicap" if market_type == "handicap" else "home"
        if away and (low == away or low in away):
            return "away_handicap" if market_type == "handicap" else "away"
        return None

    async def get_odds(self, match_id: str) -> list[Odd]:
        sport_key = self._events_by_id.get(str(match_id))
        if not sport_key:
            # Пытаемся найти спорт-ключ по всем известным (стоит запросов — кэшируем).
            for candidate in set(THE_ODDS_API_SPORT_KEYS.values()):
                try:
                    events = await self._get(
                        f"/sports/{candidate}/odds",
                        {"regions": "eu", "markets": "h2h", "oddsFormat": "decimal"},
                    )
                except SourceError:
                    continue
                if isinstance(events, list):
                    for event in events:
                        if isinstance(event, dict) and str(event.get("id")) == str(match_id):
                            sport_key = candidate
                            self._events_by_id[str(match_id)] = candidate
                            break
                if sport_key:
                    break
        if not sport_key:
            raise SourceError(f"{self.provider_name}: спорт-ключ для события {match_id} не найден")

        async def factory() -> list[Odd]:
            payload = await self._get(
                f"/sports/{sport_key}/odds",
                {"regions": "eu", "markets": "h2h,totals,spreads", "oddsFormat": "decimal", "eventIds": match_id},
            )
            events = payload if isinstance(payload, list) else []
            all_odds: list[Odd] = []
            for event in events:
                all_odds.extend(self._parse_bookmakers(event))
            if not all_odds:
                logger.warning("{}: кэфы для {} пусты (возможно, событие в другой лиге)", self.provider_name, match_id)
            return all_odds

        return await self._cache.get_or_set(f"odds:{match_id}", factory, ttl_sec=10 * 60)

    async def get_live_odds(self, match_id: str) -> list[Odd]:
        # Live-линии в free-тарифе The Odds API отсутствуют — честно сообщаем об этом.
        raise SourceError(f"{self.provider_name}: live-линии недоступны в бесплатном тарифе")

    async def get_sports(self) -> list[dict[str, Any]]:
        """Служебный метод для scripts/check_sources.py: список спортов и остаток лимита."""
        payload = await self._get("/sports", {})
        return payload if isinstance(payload, list) else []
