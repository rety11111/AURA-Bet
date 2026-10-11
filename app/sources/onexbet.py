"""1xBet API (RapidAPI, провайдер cosmx) — коэффициенты, включая киберспорт.

Зачем: на free-тарифе The Odds API нет CS2/Dota 2 и он быстро выжигается; Winline/BetBoom
работают только при вручную добытых URL из DevTools. 1xBet через RapidAPI даёт живые линии
(прематч и лайв) без скрейпинга — в том числе по киберспорту.

Контракт (по документации провайдера в RapidAPI Playground):
  * хост: `1xbet-api.p.rapidapi.com` (сверьте в Playground, если не работает);
  * заголовки: `X-RapidAPI-Key`, `X-RapidAPI-Host` (как у API-Football/Basketball/Tennis);
  * `GET /sports` → [{id, name}, ...];
  * `GET /sports/{sport_id}/leagues` → лиги спорта;
  * `GET /matches?league_id=&sport_id=` → события лиги;
  * `GET /matches/{match_id}/markets` (и `/markets_v2`) → рынки одного события;
  * `mode=line|live` — режим линии (прематч/лайв). Использовать его надо согласованно:
    событие, взятое в `live`, требует `mode=live` и для рынков, иначе 404.

⚠️ ЛИМИТЫ: free-план RapidAPI — 500 запросов/месяц. Поэтому:
  * каталог спортов/лиг кэшируется (24 ч), расписание лиги — 30 мин, рынки матча — 10 мин;
  * есть месячный бюджет (ONEXBET_MONTHLY_LIMIT, дефолт 400): исчерпали — источник молчит
    до конца окна;
  * обход лиг ограничен (ONEXBET_MAX_LEAGUES_PER_SPORT) — у футбола сотни лиг, и «пройти
    все» на free-плане невозможно. Нужные лиги задаются явно через ONEXBET_LEAGUES.

Схема рынков (`markets_v2`): `{"base": -3.5, "market_id": 2, "name": "Handicap",
"odds": {"Handicap HOME ": 14.5}}` — имя рынка и названия исходов разбираются терпимо
(кириллица/латиница, «W1»/«1»/название команды), потому что строки у провайдера плавают.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from loguru import logger

from app.config import settings
from app.sources.base import (
    Match,
    MonthlyBudget,
    Odd,
    OddsProvider,
    SourceError,
    TTLCache,
    safe_float,
    safe_int,
)

# Ключевые слова для авто-поиска sport_id в каталоге 1xBet (можно переопределить env).
SPORT_NAME_HINTS: dict[str, tuple[str, ...]] = {
    "football": ("football", "soccer", "футбол"),
    "hockey": ("ice hockey", "hockey", "хоккей"),
    "basketball": ("basketball", "баскетбол"),
    "tennis": ("tennis", "теннис"),
    "mma": ("mma", "mixed martial", "ufc"),
    "boxing": ("boxing", "бокс"),
    "cs2": ("counter-strike", "counter strike", "cs2", "csgo"),
    "dota2": ("dota",),
}
DRAW_KEYS = ("x", "draw", "ничья", "х", "d")
OVER_KEYS = ("over", "больше", "тотал больше", "total over")
UNDER_KEYS = ("under", "меньше", "тотал меньше", "total under")


def _parse_start(raw: Any) -> datetime | None:
    if raw in (None, ""):
        return None
    if isinstance(raw, (int, float)):
        value = float(raw)
        if value > 1e11:  # миллисекунды
            value /= 1000.0
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(raw).strip().replace("Z", "+00:00")
    if text.isdigit():
        return _parse_start(int(text))
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _pick(row: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in row and row[key] not in (None, ""):
            return row[key]
    return None


def _normalize_key(raw: Any) -> str:
    return " ".join(str(raw or "").strip().lower().replace("ё", "е").split())


class OnexbetOddsProvider(OddsProvider):
    """Один источник коэффициентов: 1xBet через RapidAPI (прематч + лайв)."""

    provider_name = "1xbet"

    def __init__(self, api_key: str | None = None, host: str | None = None, **kwargs: Any) -> None:
        self.api_key = (api_key if api_key is not None else settings.rapidapi_key or "").strip()
        self.host = (host or settings.onexbet_rapidapi_host or "").strip()
        super().__init__(
            source_name=self.provider_name,
            base_url=f"https://{self.host}" if self.host else "",
            headers={"X-RapidAPI-Key": self.api_key, "X-RapidAPI-Host": self.host},
            **kwargs,
        )
        self._cache = TTLCache(ttl_sec=settings.onexbet_cache_ttl_sec, maxsize=512)
        self._budget = MonthlyBudget(settings.onexbet_monthly_limit)
        self._disabled_reason: str | None = None
        self._last_failure: str | None = None
        self._sport_ids: dict[str, int] = {}
        self._sport_id_hints: dict[str, int] = {}
        self._leagues_by_sport: dict[str, list[dict[str, Any]]] = {}

    # ------------------------------------------------------------- доступность
    @property
    def available(self) -> bool:
        if self._disabled_reason:
            return False
        if not settings.onexbet_enabled:
            return False
        if not self.api_key or not self.host:
            self._disable(
                "нужны RAPIDAPI_KEY и ONEXBET_RAPIDAPI_HOST (сверьте хост в RapidAPI Playground)"
            )
            return False
        return True

    @property
    def disabled_reason(self) -> str | None:
        return self._disabled_reason

    @property
    def supported_sports(self) -> tuple[str, ...]:  # type: ignore[override]
        """Опрашиваем только настроенные виды спорта (иначе бесплатно не получится).

        По умолчанию — киберспорт (ради него источник и подключается), расширяется
        через ONEXBET_SPORT_IDS/ONEXBET_LEAGUES.
        """
        configured = [sport for sport in settings.onexbet_leagues_map if sport in SPORT_NAME_HINTS]
        if configured:
            return tuple(dict.fromkeys(configured))
        if settings.onexbet_sport_ids_map:
            return tuple(dict.fromkeys(settings.onexbet_sport_ids_map))
        defaults = tuple(sport for sport in ("cs2", "dota2") if sport in SPORT_NAME_HINTS)
        return defaults or tuple(SPORT_NAME_HINTS)

    @property
    def budget_remaining(self) -> int:
        return self._budget.remaining

    def _disable(self, reason: str) -> None:
        if self._disabled_reason == reason:
            return
        self._disabled_reason = reason
        logger.warning("1xbet: источник отключён до рестарта — {}", reason)

    def _fail(self, message: str) -> None:
        if self._last_failure == message:
            logger.debug("1xbet: повтор сбоя — {}", message)
            return
        self._last_failure = message
        logger.warning("1xbet: {}", message)

    # ------------------------------------------------------------------ запросы
    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        if not self.available:
            return None
        if not self._budget.take():
            self._fail(
                f"месячный бюджет исчерпан (ONEXBET_MONTHLY_LIMIT={self._budget.limit}) — "
                "новые запросы до конца окна не отправляем"
            )
            return None
        try:
            payload = await self.get_json(f"{self.base_url}{path}", params=params or {})
        except SourceError as exc:
            message = str(exc)
            if "401" in message or "403" in message:
                self._disable(
                    f"RapidAPI отклонил ключ или подписку на 1xBet API (проверьте RAPIDAPI_KEY "
                    f"и подписку Basic в Playground): {message[:200]}"
                )
            elif "404" in message:
                self._fail(f"404 на {path} — проверьте путь/режим (line|live) в RapidAPI Playground")
            else:
                self._fail(f"запрос {path} не удался: {message[:200]}")
            return None
        except Exception as exc:  # noqa: BLE001 — источник не должен ронять пайплайн
            self._fail(f"неожиданная ошибка запроса {path}: {exc}")
            return None
        self._last_failure = None
        if isinstance(payload, dict) and payload.get("status") == "error":
            self._fail(f"API вернул ошибку на {path}: {str(payload.get('message'))[:200]}")
            return None
        if isinstance(payload, dict) and "data" in payload:
            return payload.get("data")
        return payload

    # -------------------------------------------------------------- каталоги
    async def _sport_id(self, sport: str) -> int | None:
        """sport_id в 1xBet: сначала из env, иначе ищем в каталоге /sports по названию."""
        configured = settings.onexbet_sport_ids_map.get(sport)
        if configured is not None:
            return safe_int(configured)
        if sport in self._sport_ids:
            return self._sport_ids[sport]

        async def factory() -> list[dict[str, Any]]:
            data = await self._get("/sports")
            if isinstance(data, list):
                return [row for row in data if isinstance(row, dict)]
            if isinstance(data, dict) and isinstance(data.get("sports"), list):
                return [row for row in data["sports"] if isinstance(row, dict)]
            return []

        catalog = await self._cache.get_or_set("sports", factory, ttl_sec=settings.onexbet_catalog_ttl_sec)
        hints = SPORT_NAME_HINTS.get(sport, (sport,))
        for row in catalog:
            name = _normalize_key(row.get("name") or row.get("sport") or row.get("title"))
            sport_id = safe_int(row.get("id") or row.get("sport_id"))
            if sport_id is None or not name:
                continue
            if any(hint in name for hint in hints):
                self._sport_ids[sport] = sport_id
                return sport_id
        self._fail(
            f"не нашёл sport_id для '{sport}' в каталоге 1xBet — задайте ONEXBET_SPORT_IDS "
            '({"cs2": 123456}) по данным GET /sports'
        )
        return None

    async def _leagues(self, sport: str) -> list[dict[str, Any]]:
        """Лиги спорта: из env (ONEXBET_LEAGUES) или из каталога API."""
        explicit = settings.onexbet_leagues_map.get(sport)
        if explicit:
            return [{"id": league_id} for league_id in explicit]
        if sport in self._leagues_by_sport:
            return self._leagues_by_sport[sport]
        sport_id = await self._sport_id(sport)
        if sport_id is None:
            return []

        async def factory() -> list[dict[str, Any]]:
            data = await self._get(f"/sports/{sport_id}/leagues")
            if isinstance(data, list):
                return [row for row in data if isinstance(row, dict)]
            if isinstance(data, dict) and isinstance(data.get("leagues"), list):
                return [row for row in data["leagues"] if isinstance(row, dict)]
            return []

        leagues = await self._cache.get_or_set(
            f"leagues:{sport_id}", factory, ttl_sec=settings.onexbet_catalog_ttl_sec
        )
        self._leagues_by_sport[sport] = leagues
        return leagues

    # -------------------------------------------------------------- расписание
    async def get_upcoming(self, sport: str, day: date) -> list[Match]:
        if sport not in self.supported_sports:
            logger.debug("1xbet: спорт '{}' не в списке источника — пропускаем", sport)
            return []
        leagues = await self._leagues(sport)
        if not leagues:
            return []
        limit = max(1, int(settings.onexbet_max_leagues_per_sport))
        matches: list[Match] = []
        for league in leagues[:limit]:
            league_id = safe_int(league.get("id") or league.get("league_id"))
            if league_id is None:
                continue
            rows = await self._cache.get_or_set(
                f"matches:{sport}:{league_id}:{day.isoformat()}",
                lambda league_id=league_id: self._matches_for_league(sport, league_id, day),
                ttl_sec=settings.onexbet_cache_ttl_sec,
            )
            matches.extend(rows)
        logger.info("1xbet: {} на {} → {} событий (лиг опрошено: {})", sport, day, len(matches), min(limit, len(leagues)))
        return matches

    async def _matches_for_league(self, sport: str, league_id: int, day: date) -> list[Match]:
        sport_id = await self._sport_id(sport) or 0
        data = await self._get("/matches", {"league_id": league_id, "sport_id": sport_id})
        rows: list[Any] = []
        if isinstance(data, list):
            rows = data
        elif isinstance(data, dict):
            for key in ("matches", "games", "events", "list"):
                if isinstance(data.get(key), list):
                    rows = data[key]
                    break
        matches: list[Match] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            ext_id = _pick(row, ("id", "match_id", "game_id", "event_id"))
            home = _pick(row, ("team1", "home", "team1_name", "home_team", "team1name"))
            away = _pick(row, ("team2", "away", "team2_name", "away_team", "team2name"))
            starts_at = _parse_start(_pick(row, ("dateStart", "date_start", "start_time", "startsAt", "time", "date")))
            if ext_id is None or not home or not away or starts_at is None:
                continue
            if starts_at.date() != day:
                continue
            league_name = str(_pick(row, ("league_name", "league", "tournament")) or f"league {league_id}")
            matches.append(
                Match(
                    ext_id=str(ext_id),
                    sport=sport,
                    league=league_name,
                    home_team=str(home),
                    away_team=str(away),
                    starts_at=starts_at,
                    source=self.provider_name,
                    home_score=safe_int(_pick(row, ("score1", "team1_score", "home_score"))),
                    away_score=safe_int(_pick(row, ("score2", "team2_score", "away_score"))),
                    extra={"onexbet_league_id": league_id},
                )
            )
        return matches

    # -------------------------------------------------------------------- кэфы
    async def get_odds(self, match_id: str) -> list[Odd]:
        return await self._odds(match_id, mode="line")

    async def get_live_odds(self, match_id: str) -> list[Odd]:
        return await self._odds(match_id, mode="live")

    async def _odds(self, match_id: str, mode: str) -> list[Odd]:
        """Рынки одного события. `mode` обязателен и должен совпадать с тем, откуда взяли id."""

        async def factory() -> list[Odd]:
            data = await self._get(f"/matches/{match_id}/markets_v2", {"mode": mode})
            rows = data if isinstance(data, list) else (data or {}).get("markets") if isinstance(data, dict) else None
            if not isinstance(rows, list):
                data = await self._get(f"/matches/{match_id}/markets", {"mode": mode})
                rows = data if isinstance(data, list) else (data or {}).get("markets") if isinstance(data, dict) else []
            return parse_markets(rows or [], source=self.provider_name)

        odds = await self._cache.get_or_set(f"odds:{mode}:{match_id}", factory, ttl_sec=settings.onexbet_odds_ttl_sec)
        if not odds:
            logger.debug("1xbet: кэфы по {} ({}) отсутствуют", match_id, mode)
        return odds


def parse_markets(rows: list[Any], source: str = "1xbet") -> list[Odd]:
    """Разбирает рынки 1xBet в наши Odd.

    Формат строки: {"base": 2.5|null, "market_id": 3, "name": "Total",
    "odds": {"Over": 1.9, "Under": 1.9}} — ключи `odds` у провайдера плавают
    («Total Over 2.5», «Тотал Больше 2.5», «W1», «1», название команды), поэтому
    исход определяется по тексту, а линия — по `base` или числу в названии.
    """
    odds: list[Odd] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        market_name = _normalize_key(row.get("name") or row.get("market_name") or row.get("group"))
        base = safe_float(row.get("base") or row.get("line") or row.get("handicap") or row.get("total"))
        options = row.get("odds") or row.get("outcomes") or row.get("selections") or {}
        if isinstance(options, list):  # иногда отдают список [{name, value/price}]
            options = {
                str(item.get("name") or item.get("key")): item.get("value") or item.get("price") or item.get("odds")
                for item in options
                if isinstance(item, dict)
            }
        if not isinstance(options, dict):
            continue
        market_type = _market_type(market_name, base)
        if market_type is None:
            continue
        for raw_selection, raw_price in options.items():
            price = safe_float(raw_price if not isinstance(raw_price, dict) else raw_price.get("value"))
            if not price or price <= 1.0:
                continue
            selection = _selection(market_type, _normalize_key(raw_selection))
            if selection is None:
                continue
            line = base
            if line is None and market_type in ("totals", "handicap"):
                line = _line_from_text(str(raw_selection))
            odds.append(
                Odd(market=market_type, selection=selection, line=line, price=price, source=source)
            )
    return odds


def _market_type(name: str, base: float | None) -> str | None:
    if not name:
        # Рынок без имени — ориентируемся на то, есть ли число (тотал/фора).
        return "totals" if base is not None else None
    if "double chance" in name or "двойной шанс" in name or "1x2 double" in name:
        return "double_chance"
    if "handicap" in name or "фора" in name:
        return "handicap"
    if "total" in name or "тотал" in name or "over/under" in name or "больше" in name or "меньше" in name:
        return "totals"
    if name in ("1x2", "match result", "winner", "исход") or "match winner" in name or "1 x 2" in name:
        return "1x2"
    if "moneyline" in name or "w1w2" in name:
        return "1x2"
    return None


def _selection(market_type: str, key: str) -> str | None:
    """Определяет исход по ключу в `odds` (латиница/кириллица, «П1», «Handicap HOME»)."""
    if not key:
        return None
    # Кириллические «х»/«с» приводим к латинским аналогам: провайдер мешает алфавиты
    # в одном ключе («Х2», «1Х») — иначе ничья/двойной шанс не распознаются.
    key = key.replace("х", "x").replace("с", "c")
    compact = key.replace(" ", "")
    if market_type == "double_chance":
        if key in ("home or x", "1x", "home or draw", "п1х", "1х"):
            return "1x"
        if key in ("home or away", "12", "1 2"):
            return "12"
        if key in ("away or x", "x2", "draw or away", "х2", "2х"):
            return "x2"
        return None
    if market_type == "totals":
        if any(marker in key for marker in OVER_KEYS) or compact.startswith("тб"):
            return "over"
        if any(marker in key for marker in UNDER_KEYS) or compact.startswith("тм"):
            return "under"
        return None
    if key in DRAW_KEYS:
        return "draw"
    # У форы ключ бывает «Handicap HOME»/«Фора 1» — сначала убираем слово «фора».
    cleaned = key
    for word in ("handicap", "фора", "hcp", "азиатская", "asian"):
        cleaned = cleaned.replace(word, " ")
    cleaned = " ".join(cleaned.split())
    compact_clean = cleaned.replace(" ", "")
    if compact in ("1", "w1", "p1", "п1") or compact_clean in ("1", "home", "хозяева", "team1"):
        return "home_handicap" if market_type == "handicap" else "home"
    if compact in ("2", "w2", "p2", "п2") or compact_clean in ("2", "away", "гости", "team2"):
        return "away_handicap" if market_type == "handicap" else "away"
    return None


def _line_from_text(text: str) -> float | None:
    """Пытается вытащить линию из названия исхода: «Total Over 2.5», «Тотал Больше 2.5»."""
    for token in str(text).replace(",", ".").split():
        value = safe_float(token)
        if value is not None and abs(value) < 100 and value != 0:
            return value
    return None
