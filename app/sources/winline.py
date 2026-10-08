"""WinlineOddsProvider — парсинг публичного JSON-API сайта (прематч + лайв).

⚠️ ЧЕСТНО О ЭНДПОИНТАХ ⚠️
Настоящие URL Winline (и BetBoom) НЕ захардкожены и в этом файле, и в проекте
вообще: они меняются, зависят от региона/CDN и не могут быть достоверно известны
автору кода. Адреса берутся ТОЛЬКО из окружения:
    WINLINE_API_BASE, WINLINE_LIVE_API_BASE
Если переменная пуста — провайдер считается недоступным, и OddsAggregator
работает на остальных источниках (правило деградации).

Как добыть URL через DevTools браузера — пошагово написано в SETUP.md
(раздел «Как достать реальные URL Winline и BetBoom»).

КАК УСТРОЕН ПАРСЕР
Найденный вами в браузере JSON разбирается эвристически, по маппингам полей,
которые описаны ниже константами (FIELD_MAPPING / MARKET_KEYWORDS /
SELECTION_KEYWORDS). Это сделано намеренно: точная схема ответа букмекера
автору неизвестна, но все типовые варианты («events»/«matches»/«data»,
«price»/«coef»/«kf», «1»/«П1»/«W1») перечислены в маппингах. Если после
подстановки URL в логе видно «событий не найдено» — сверьте фактический JSON
в браузере с маппингами и при необходимости расширьте списки ниже.
Скрипт scripts/check_sources.py печатает, сколько событий/кэфов удалось разобрать,
— это самый быстрый способ проверить маппинги.

Класс JsonBookmakerOddsProvider — общий для Winline и BetBoom (BetBoom переиспользует
его, см. sources/betboom.py, как «аналогичный парсинг» из ТЗ).
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

from loguru import logger

from app.config import settings
from app.sources.base import (
    Match,
    Odd,
    OddsProvider,
    SourceError,
    deep_find_list,
    pick,
    safe_float,
    safe_int,
)

# --------------------------------------------------------------------------- #
# МАППИНГИ ПОЛЕЙ (кандидаты имён ключей в JSON букмекера)
# --------------------------------------------------------------------------- #
FIELD_MAPPING: dict[str, tuple[str, ...]] = {
    "events": ("events", "matches", "data", "items", "result", "results", "games", "coupons", "list", "eventList"),
    "event_id": ("id", "eventId", "event_id", "matchId", "match_id", "couponId", "gameId"),
    "home": ("home", "homeTeam", "team1", "first", "host", "home_team", "homeName", "teamA", "opponent1"),
    "away": ("away", "awayTeam", "team2", "second", "guest", "away_team", "awayName", "teamB", "opponent2"),
    "team_name": ("name", "title", "ruName", "enName", "teamName", "shortName", "displayName", "value"),
    "start_time": ("start", "startTime", "startsAt", "time", "date", "begin", "eventTime", "startDate", "start_time"),
    "league": ("league", "leagueName", "championship", "tournament", "category", "competition", "group"),
    "league_name": ("name", "title", "ruName", "enName", "displayName"),
    "sport": ("sport", "sportName", "sportType", "discipline"),
    "markets": ("markets", "odds", "oddsTypes", "market", "marketList", "groups", "outcomes", "bets"),
    "market_name": ("name", "title", "ruName", "marketName", "type", "group", "key", "kind", "caption"),
    "selections": ("outcomes", "odds", "values", "selections", "outcome", "items", "bets", "coeffs", "prices"),
    "selection_name": ("name", "title", "ruName", "shortName", "outcomeName", "type", "key", "value"),
    "price": ("price", "coef", "kf", "koef", "odds", "value", "cf", "k", "coefficient", "rate"),
    "handicap": ("handicap", "hdp", "param", "line", "spread", "fora", "value"),
    "score_home": ("homeScore", "scoreHome", "score1", "home_score", "firstScore"),
    "score_away": ("awayScore", "scoreAway", "score2", "away_score", "secondScore"),
    "stage": ("stage", "period", "status", "state", "phase", "timer", "currentPeriod", "minute"),
}

# Слова-маркеры типа рынка (ищутся в названии рынка И в названиях исходов).
# Порядок важен: частные случаи (тотал/фора/двойной шанс) идут раньше общего 1X2.
MARKET_KEYWORDS: dict[str, tuple[str, ...]] = {
    "totals": ("тотал", "total", "больше/меньше", "over/under", "o/u", "тоталы"),
    "handicap": ("фора", "handicap", "азиат", "spread", "asian", "hdp", "ф1", "ф2"),
    "double_chance": ("двойной шанс", "double chance", "1x2 двойной", "dc", "двойной"),
    "1x2": ("1x2", "исход", "победа", "moneyline", "main", "result", "основной", "1 x 2", "winner", "regulation"),
}

# Маппинг названий исходов → (selection, знак линии).
SELECTION_KEYWORDS: dict[str, str] = {
    "home": "home",
    "away": "away",
    "draw": "draw",
    "over": "over",
    "under": "under",
    "home_handicap": "home_handicap",
    "away_handicap": "away_handicap",
    "dc_1x": "1x",
    "dc_12": "12",
    "dc_x2": "x2",
}

_NUM_RE = re.compile(r"[-+]?\d+(?:[.,]\d+)?")


def _norm(text: Any) -> str:
    return str(text or "").strip().lower()


def _extract_entity_name(node: Any) -> str | None:
    """Достаёт название команды/турнира из строки или вложенного объекта."""
    if isinstance(node, str):
        return node.strip() or None
    if isinstance(node, dict):
        value = pick(node, FIELD_MAPPING["team_name"])
        if value is None:
            # Некоторые API отдают {"ru": "Спартак", "en": "Spartak"}
            for key in ("ru", "ruName", "en", "name"):
                if node.get(key):
                    return str(node[key]).strip()
            return None
        return _extract_entity_name(value)
    return None


def _parse_datetime(value: Any, default_tz_offset_hours: int = 0) -> datetime | None:
    """Парсит время из ISO-строки или epoch (сек/мс)."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e11:  # миллисекунды
            ts /= 1000.0
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return _parse_datetime(int(text))
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            # Букмекеры часто отдают локальное время без tz — считаем, что это МСК-подобный офсет
            # (настраивается через default_tz_offset_hours, по умолчанию 0 = UTC).
            parsed = parsed - timedelta(hours=default_tz_offset_hours)
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%d.%m.%Y %H:%M", "%d.%m.%Y", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
            return parsed - timedelta(hours=default_tz_offset_hours)
        except ValueError:
            continue
    return None


def _market_type(market_name: str, selection_names: list[str]) -> str | None:
    """Определяет тип рынка по названию рынка И названиям исходов."""
    name = _norm(market_name)
    joined = " ".join(_norm(s) for s in selection_names)
    haystack = f"{name} {joined}"
    for market, keywords in MARKET_KEYWORDS.items():
        if any(kw in haystack for kw in keywords):
            return market
    # Фоллбэк по структуре исходов
    has_over = any(x in joined for x in ("over", "больше", "тб", "бол"))
    has_under = any(x in joined for x in ("under", "меньше", "тм", "мен"))
    if has_over and has_under:
        return "totals"
    has_draw = any(x in joined.split() for x in ("x", "х", "draw", "ничья"))
    has_home = any(x in joined for x in ("п1", "w1", "home", "хозяев", "1-й", "победа 1"))
    has_away = any(x in joined for x in ("п2", "w2", "away", "гост", "2-й", "победа 2"))
    if (has_home and has_away) or (has_draw and has_home):
        return "1x2"
    return None


_HANDICAP_SIDE_RE = re.compile(r"(?:ф|f|п|w|hdp|handicap|h)\s*([12])(?![0-9.,])")


def _numbers_in(text: str) -> list[float]:
    out: list[float] = []
    for match in _NUM_RE.findall(text):
        try:
            out.append(float(match.replace(",", ".")))
        except ValueError:
            continue
    return out


def _selection_and_line(
    selection_name: str, market: str, handicap_hint: float | None = None, market_hint: float | None = None
) -> tuple[str, float | None] | None:
    """Возвращает (selection, line) для названия исхода внутри рынка.

    handicap_hint — значение из поля handicap/spread/line конкретного исхода,
    market_hint — число из названия рынка (например «Тотал 2.5» или «Фора 1 (-1.5)»).
    """
    text = _norm(selection_name)
    numbers = _numbers_in(text)
    line = handicap_hint if handicap_hint is not None else (numbers[-1] if numbers else market_hint)

    if market == "double_chance":
        compact = text.replace(" ", "").replace("х", "x")
        if compact in ("1x", "x1"):
            return SELECTION_KEYWORDS["dc_1x"], None
        if compact in ("12", "21"):
            return SELECTION_KEYWORDS["dc_12"], None
        if compact in ("x2", "2x"):
            return SELECTION_KEYWORDS["dc_x2"], None
        if "1x" in compact:
            return SELECTION_KEYWORDS["dc_1x"], None
        if "x2" in compact:
            return SELECTION_KEYWORDS["dc_x2"], None
        if "12" in compact:
            return SELECTION_KEYWORDS["dc_12"], None
        return None

    if market == "totals":
        if any(k in text for k in ("over", "бол", "тб")):
            return SELECTION_KEYWORDS["over"], line
        if any(k in text for k in ("under", "мен", "тм")):
            return SELECTION_KEYWORDS["under"], line
        return None

    if market == "handicap":
        # Сначала точные формы ("1", "п2", "away"), затем «Ф1 -1.5» (проверяем 2 раньше 1,
        # иначе «+1.5» в названии второй форы даёт ложное срабатывание).
        if text in ("2", "п2", "w2", "away", "гости", "гость", "2-й", "2-я"):
            return SELECTION_KEYWORDS["away_handicap"], handicap_hint if handicap_hint is not None else line
        if text in ("1", "п1", "w1", "home", "хозяева", "хозяин", "1-й", "1-я"):
            return SELECTION_KEYWORDS["home_handicap"], handicap_hint if handicap_hint is not None else line
        side = _HANDICAP_SIDE_RE.search(text)
        if side:
            target = SELECTION_KEYWORDS["away_handicap"] if side.group(1) == "2" else SELECTION_KEYWORDS["home_handicap"]
            value = handicap_hint if handicap_hint is not None else (numbers[-1] if numbers else market_hint)
            if value is None:
                return None
            # «Фора -1.5» без указания стороны — не гадаем.
            return target, value
        if "away" in text or "гост" in text:
            return SELECTION_KEYWORDS["away_handicap"], line
        if "home" in text or "хозя" in text:
            return SELECTION_KEYWORDS["home_handicap"], line
        return None

    # 1x2 / h2h
    if text in ("1", "п1", "w1", "home", "1-й", "хозяева", "победа 1", "победа хозяев") or text.startswith("1 "):
        return SELECTION_KEYWORDS["home"], None
    if text in ("x", "х", "draw", "ничья", "n") or "ничья" in text:
        return SELECTION_KEYWORDS["draw"], None
    if text in ("2", "п2", "w2", "away", "2-й", "гости", "победа 2", "победа гостей") or text.startswith("2 "):
        return SELECTION_KEYWORDS["away"], None
    if "home" in text or "хозя" in text:
        return SELECTION_KEYWORDS["home"], None
    if "away" in text or "гост" in text:
        return SELECTION_KEYWORDS["away"], None
    if "draw" in text or "нич" in text:
        return SELECTION_KEYWORDS["draw"], None
    return None


def _extract_price(node: Any) -> float | None:
    if isinstance(node, (int, float)):
        price = float(node)
        return price if price > 1.0 else None
    if not isinstance(node, dict):
        return None
    for key in FIELD_MAPPING["price"]:
        if key in node:
            value = node[key]
            if isinstance(value, dict):
                value = pick(value, FIELD_MAPPING["price"])
            price = safe_float(value)
            if price and price > 1.0:
                return price
    # Иногда цена лежит как {"odds": {"home": {"price": 2.1}}}
    for key in ("price", "coef", "kf", "odds", "value"):
        nested = node.get(key)
        if isinstance(nested, dict):
            price = _extract_price(nested)
            if price:
                return price
    return None


def _first_list(node: dict[str, Any], keys: tuple[str, ...]) -> list[Any] | None:
    for key in keys:
        value = node.get(key)
        if isinstance(value, list) and value:
            return value
    return None


def parse_odds_payload(payload: Any, source: str) -> list[Odd]:
    """Достаёт коэффициенты из произвольного JSON букмекера.

    Алгоритм: обходим дерево, находим узлы-рынки (имя рынка + список исходов),
    классифицируем рынок, нормализуем названия исходов и линии, вытаскиваем цену.
    Всё, что не распознано, игнорируется — не гадаем.
    """
    odds: list[Odd] = []
    seen: set[tuple[str, str, float | None]] = set()

    def add(market: str, selection: str, line: float | None, price: float) -> None:
        key = (market, selection, line)
        if key in seen or price <= 1.0:
            return
        seen.add(key)
        odds.append(Odd(market=market, selection=selection, line=line, price=price, source=source))

    def walk(node: Any, depth: int = 0) -> None:
        if depth > 8:
            return
        if isinstance(node, dict):
            market_name = str(pick(node, FIELD_MAPPING["market_name"]) or "")
            selections_raw = _first_list(node, FIELD_MAPPING["selections"]) or _first_list(node, FIELD_MAPPING["markets"])
            if selections_raw:
                names: list[str] = []
                # (имя исхода, подсказка линии, цена)
                parsed: list[tuple[str, float | None, float | None]] = []
                for item in selections_raw:
                    if not isinstance(item, dict):
                        continue
                    name_raw = str(pick(item, FIELD_MAPPING["selection_name"]) or "")
                    hint = safe_float(pick(item, FIELD_MAPPING["handicap"]))
                    price = _extract_price(item)
                    nested = _first_list(item, FIELD_MAPPING["selections"])
                    if nested:
                        # {"name": "Тотал", "outcomes": [{"name": "Over 2.5", "price": 1.9}, ...]}
                        names.append(name_raw)
                        for sub in nested:
                            if not isinstance(sub, dict):
                                continue
                            sub_name = str(pick(sub, FIELD_MAPPING["selection_name"]) or name_raw)
                            sub_hint = safe_float(pick(sub, FIELD_MAPPING["handicap"]))
                            sub_price = _extract_price(sub)
                            names.append(sub_name)
                            parsed.append((sub_name, sub_hint if sub_hint is not None else hint, sub_price))
                    else:
                        names.append(name_raw)
                        parsed.append((name_raw, hint, price))

                market = _market_type(market_name, names)
                if market:
                    market_numbers = _numbers_in(market_name)
                    market_hint = market_numbers[-1] if market_numbers else None
                    for name, hint, price in parsed:
                        if price is None:
                            continue
                        resolved = _selection_and_line(name, market, hint, market_hint)
                        if resolved:
                            add(market, resolved[0], resolved[1], price)
            for value in node.values():
                if isinstance(value, (dict, list)):
                    walk(value, depth + 1)
        elif isinstance(node, list):
            for value in node:
                walk(value, depth + 1)

    walk(payload)
    return odds


class JsonBookmakerOddsProvider(OddsProvider):
    """Общий парсер букмекера, у которого есть JSON на прематч и на лайв.

    Наследники задают:
      * provider_name / source_name — имя источника в БД (odds.source);
      * base_url / live_base_url — из env (см. config.py);
      * sport_params — маппинг sport → query-параметры (значения из DevTools);
      * tz_offset_hours — сдвиг локального времени в ответе, если оно без таймзоны.
    """

    provider_name = "bookmaker"
    live_base_url = ""
    tz_offset_hours = 0
    sport_params: dict[str, dict[str, str]] = {}
    live_sport_params: dict[str, dict[str, str]] = {}

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(source_name=self.provider_name, politeness=True, **kwargs)

    @property
    def available(self) -> bool:
        return bool(self.base_url or self.live_base_url)

    @property
    def live_available(self) -> bool:
        return bool(self.live_base_url)

    # ------------------------------------------------------------- prematch
    def _event_params(self, sport: str, day: date | None = None) -> dict[str, str]:
        params = dict(settings.extra_params(self._extra_params_raw))
        params.update(self.sport_params.get(sport, {}))
        if day is not None:
            params.setdefault("date", day.isoformat())
        return params

    @property
    def _extra_params_raw(self) -> str:
        return ""

    async def get_upcoming(self, sport: str, day: date) -> list[Match]:
        if not self.base_url:
            raise SourceError(f"{self.provider_name}: URL не задан (см. SETUP.md → DevTools)")
        payload = await self.get_json(self.base_url, params=self._event_params(sport, day))
        matches = self.parse_matches_payload(payload, sport=sport)
        logger.info("{}: прематч {} на {} → {} событий", self.provider_name, sport, day.isoformat(), len(matches))
        return matches

    async def get_live_events(self, sport: str) -> list[Match]:
        """Лайв-события спорта (для воркера киберспорта и обновления лайв-кэфов)."""
        if not self.live_base_url:
            raise SourceError(f"{self.provider_name}: live URL не задан")
        params = dict(settings.extra_params(self._live_params_raw))
        params.update(self.live_sport_params.get(sport, {}))
        payload = await self.get_json(self.live_base_url, params=params)
        matches = self.parse_matches_payload(payload, sport=sport, live=True)
        logger.info("{}: лайв {} → {} событий", self.provider_name, sport, len(matches))
        return matches

    @property
    def _live_params_raw(self) -> str:
        return ""

    # ----------------------------------------------------------------- odds
    async def _odds_from(self, base: str, match_id: str, params_raw: str) -> list[Odd]:
        if not base:
            raise SourceError(f"{self.provider_name}: URL не задан (см. SETUP.md → DevTools)")
        params = dict(settings.extra_params(params_raw))
        params.setdefault("event_id", match_id)
        # Вариант 1: id в пути (/events/12345). Вариант 2: id в query (?event_id=...).
        try:
            payload = await self.get_json(f"{base.rstrip('/')}/{match_id}", params=params)
        except SourceError:
            payload = await self.get_json(base, params=params)
        odds = parse_odds_payload(payload, self.provider_name)
        if not odds:
            logger.warning(
                "{}: кэфы по матчу {} не распознаны — сверьте маппинги FIELD_MAPPING с фактическим JSON",
                self.provider_name, match_id,
            )
        return odds

    async def get_odds(self, match_id: str) -> list[Odd]:
        return await self._odds_from(self.base_url, match_id, self._extra_params_raw)

    async def get_live_odds(self, match_id: str) -> list[Odd]:
        return await self._odds_from(self.live_base_url or self.base_url, match_id, self._live_params_raw)

    # -------------------------------------------------------------- parsing
    def parse_matches_payload(self, payload: Any, sport: str, live: bool = False) -> list[Match]:
        """Разбирает список событий. Возвращает только записи, где опознаны обе команды."""
        events = deep_find_list(payload, FIELD_MAPPING["events"])
        matches: list[Match] = []
        for event in events:
            if not isinstance(event, dict):
                continue
            home = _extract_entity_name(pick(event, FIELD_MAPPING["home"]))
            away = _extract_entity_name(pick(event, FIELD_MAPPING["away"]))
            if not home or not away:
                continue
            ext_id = pick(event, FIELD_MAPPING["event_id"])
            if ext_id is None:
                continue
            league = _extract_entity_name(pick(event, FIELD_MAPPING["league"])) or "unknown"
            starts_at = _parse_datetime(pick(event, FIELD_MAPPING["start_time"]), self.tz_offset_hours)
            if starts_at is None:
                starts_at = datetime.now(timezone.utc)
            score_home = safe_int(pick(event, FIELD_MAPPING["score_home"]))
            score_away = safe_int(pick(event, FIELD_MAPPING["score_away"]))
            stage = pick(event, FIELD_MAPPING["stage"])
            matches.append(
                Match(
                    ext_id=str(ext_id),
                    sport=sport,
                    league=league,
                    home_team=home,
                    away_team=away,
                    starts_at=starts_at,
                    source=self.provider_name,
                    status="live" if live else "scheduled",
                    is_live=live,
                    home_score=score_home,
                    away_score=score_away,
                    live_stage=str(stage) if stage is not None else None,
                    extra={"raw_groups": list(event.keys())[:20]},
                )
            )
        return matches


class WinlineOddsProvider(JsonBookmakerOddsProvider):
    """Основной источник кэфов: Winline (прематч + лайв, включая Dota 2 и CS2)."""

    provider_name = "winline"

    def __init__(self, base_url: str | None = None, live_base_url: str | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.base_url = (base_url if base_url is not None else settings.winline_api_base).rstrip("/")
        self.live_base_url = (live_base_url if live_base_url is not None else settings.winline_live_api_base).rstrip("/")

    @property
    def _extra_params_raw(self) -> str:
        return settings.winline_prematch_params

    @property
    def _live_params_raw(self) -> str:
        return settings.winline_live_params

    @property
    def sport_params(self) -> dict[str, dict[str, str]]:  # type: ignore[override]
        # Значения sport id у Winline нужно СВЕРИТЬ в DevTools (SETUP.md, раздел про URL):
        # обычно в запросе есть параметр вида sport/sport_id/kind. Вписывайте свои через
        # WINLINE_PREMATCH_PARAMS / WINLINE_LIVE_PARAMS (JSON) или здесь.
        return {}

    @property
    def live_sport_params(self) -> dict[str, dict[str, str]]:  # type: ignore[override]
        return {}
