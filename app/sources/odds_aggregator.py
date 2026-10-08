"""OddsAggregator (Модуль 1) — сводит линии всех доступных букмекеров по матчу.

Правила из ТЗ, реализованные здесь:
  * implied-вероятность = МЕДИАНА по источникам, ПОСЛЕ удаления маржи
    (нормализация: implied_i = (1/price_i) / Σ_j(1/price_j) внутри полного рынка);
  * value считается против ЛУЧШЕЙ цены из всех источников
    (AggregatedOutcome.best_price = максимум по провайдерам);
  * если нормализованные implied от разных источников расходятся более чем на
    MAX_SUSPICIOUS_DIVERGENCE (10%), линия помечается `suspicious`, и по ней
    сигнал не создаётся (см. pipeline/value_engine.py);
  * снимки кэфов пишутся в таблицу `odds` как append-only история (дедупликация
    по «изменилась ли цена» — настраивается ODDS_SNAPSHOT_MIN_DELTA).

Модуль намеренно разделён: чистые функции (normalize_margin, aggregate_outcomes)
тестируются юнит-тестами, а класс OddsAggregator занимается I/O и БД.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from statistics import median
from typing import Any, Iterable

from loguru import logger
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import Match, Odd as OddRow
from app.sources.base import Odd, OddsProvider, SourceError

# Какие наборы исходов образуют «полный рынок» (нужны для удаления маржи)
REQUIRED_SELECTIONS: dict[str, tuple[str, ...]] = {
    "1x2": ("home", "draw", "away"),
    "totals": ("over", "under"),
    "handicap": ("home_handicap", "away_handicap"),
    "double_chance": ("1x", "x2"),  # 12 проверяется отдельно, если присутствует
}

_TWO_WAY_1X2 = ("home", "away")


@dataclass
class AggregatedOutcome:
    """Итог агрегации по конкретному исходу рынка."""

    market: str
    selection: str
    line: float | None
    best_price: float
    best_source: str
    implied: float                       # медиана нормализованной implied-вероятности
    implied_by_source: dict[str, float] = field(default_factory=dict)
    price_by_source: dict[str, float] = field(default_factory=dict)
    suspicious: bool = False
    suspicious_reason: str | None = None
    sources: int = 0

    def as_key(self) -> tuple[str, str, float | None]:
        return (self.market, self.selection, self.line)


def group_key(odd: Odd) -> tuple[str, float | None]:
    """Ключ «рынка» внутри источника: (market, line).

    Тотал 2.5 и 3.5 — разные рынки (точное сравнение линии). Форы задаются зеркально
    (Ф1 −1.5 / Ф2 +1.5), поэтому пара собирается по МОДУЛЮ линии — иначе маржа каждой
    стороны считалась бы отдельно и implied получался бы равен 1.0.
    """
    if odd.market == "totals":
        return odd.market, odd.line
    if odd.market == "handicap":
        return odd.market, (abs(odd.line) if odd.line is not None else None)
    return odd.market, None


def normalize_margin(odds: Iterable[Odd]) -> dict[str, float]:
    """Удаляет маржу букмекера: implied_i = (1/price_i) / Σ(1/price_j).

    Возвращает {selection: вероятность}. Линии (line) в ключ не входят: вызывать
    нужно для одного рынка одного букмекера (см. group_by_source_market).
    """
    prices = {odd.selection: odd.price for odd in odds if odd.price and odd.price > 1.0}
    if not prices:
        return {}
    total = sum(1.0 / price for price in prices.values())
    if total <= 0:
        return {}
    return {selection: (1.0 / price) / total for selection, price in prices.items()}


def group_by_source_market(odds: Iterable[Odd]) -> dict[tuple[str, str, float | None], list[Odd]]:
    """Группирует кэфы: (источник, market, line) → список исходов."""
    grouped: dict[tuple[str, str, float | None], list[Odd]] = {}
    for odd in odds:
        market, line = group_key(odd)
        grouped.setdefault((odd.source, market, line), []).append(odd)
    return grouped


def _is_complete(market: str, selections: set[str]) -> bool:
    required = REQUIRED_SELECTIONS.get(market)
    if not required:
        return False
    if market == "1x2":
        # Баскетбол/теннис/киберспорт — 2-way: draw отсутствует
        if set(_TWO_WAY_1X2) <= selections:
            return True
        return set(required) <= selections
    return set(required) <= selections


def _divergent(implied_by_source: dict[str, float]) -> tuple[bool, str | None]:
    """Расхождение нормализованных implied между источниками > 10% (относительно)."""
    values = [v for v in implied_by_source.values() if v is not None]
    if len(values) < 2:
        return False, None
    low, high = min(values), max(values)
    reference = median(values)
    if reference <= 0:
        return False, None
    relative = (high - low) / reference
    absolute = high - low
    if relative > settings.max_suspicious_divergence and absolute > settings.suspicious_min_abs_diff:
        return True, (
            f"implied расходятся на {relative:.1%} "
            f"({', '.join(f'{src}: {value:.3f}' for src, value in sorted(implied_by_source.items()))})"
        )
    return False, None


def aggregate_outcomes(odds: list[Odd]) -> list[AggregatedOutcome]:
    """Чистая функция агрегации: список снимков кэфов → агрегированные исходы.

    Для каждого (market, selection, line):
      * удаляем маржу внутри каждого источника и каждого полного рынка;
      * implied = медиана по источникам;
      * best_price = лучшая (максимальная) цена среди источников;
      * фиксируем расхождения > 10% как suspicious.
    """
    grouped = group_by_source_market(odds)
    # implied_by_outcome[(market, selection, line)][source] = prob
    implied_by_outcome: dict[tuple[str, str, float | None], dict[str, float]] = {}
    price_by_outcome: dict[tuple[str, str, float | None], dict[str, float]] = {}

    for (source, market, line), items in grouped.items():
        selections = {item.selection for item in items}
        if not _is_complete(market, selections):
            # Неполный рынок → маржу не убираем (иначе получим искажённые вероятности).
            for item in items:
                if item.price and item.price > 1.0:
                    price_by_outcome.setdefault((market, item.selection, item.line), {})[source] = item.price
            continue
        normalized = normalize_margin(items)
        for item in items:
            key = (market, item.selection, item.line)
            if item.selection in normalized:
                implied_by_outcome.setdefault(key, {})[source] = normalized[item.selection]
            if item.price and item.price > 1.0:
                price_by_outcome.setdefault(key, {})[source] = item.price

    outcomes: list[AggregatedOutcome] = []
    for key, per_source_implied in implied_by_outcome.items():
        market, selection, line = key
        prices = price_by_outcome.get(key, {})
        if not prices:
            continue
        best_source, best_price = max(prices.items(), key=lambda kv: kv[1])
        suspicious, reason = _divergent(per_source_implied)
        outcomes.append(
            AggregatedOutcome(
                market=market,
                selection=selection,
                line=line,
                best_price=best_price,
                best_source=best_source,
                implied=float(median(per_source_implied.values())),
                implied_by_source=dict(per_source_implied),
                price_by_source=dict(prices),
                suspicious=suspicious,
                suspicious_reason=reason,
                sources=len(prices),
            )
        )
    return outcomes


class OddsAggregator:
    """Собирает линии со всех доступных провайдеров и пишет снимки в БД."""

    def __init__(self, providers: list[OddsProvider] | None = None) -> None:
        self.providers: list[OddsProvider] = providers or []

    def available_providers(self) -> list[OddsProvider]:
        return [provider for provider in self.providers if provider.available]

    # ------------------------------------------------------------ collection
    async def collect_for_match(self, ext_ids: dict[str, str], live: bool = False) -> list[Odd]:
        """Опрашивает провайдеров по матчу, каждый отказ изолирован (деградация)."""
        collected: list[Odd] = []
        failures: list[str] = []
        for provider in self.providers:
            if not provider.available:
                continue
            ext_id = ext_ids.get(provider.provider_name) or ext_ids.get(provider.source_name)
            if not ext_id:
                continue
            try:
                odds = await (provider.get_live_odds(ext_id) if live else provider.get_odds(ext_id))
                collected.extend(odds)
            except SourceError as exc:
                failures.append(provider.provider_name)
                logger.warning("aggregator: {} не отдал линию ({})", provider.provider_name, exc)
            except Exception as exc:  # noqa: BLE001 — источник не должен ронять пайплайн
                failures.append(provider.provider_name)
                logger.exception("aggregator: неожиданная ошибка {}: {}", provider.provider_name, exc)
        if failures:
            logger.info(
                "aggregator: матч {} — работаем без источников: {}", ext_ids, ", ".join(sorted(set(failures)))
            )
        logger.info("aggregator: собрано {} кэфов из {} источников", len(collected), len({o.source for o in collected}))
        return collected

    async def collect_upcoming(self, sport: str, day) -> list[tuple[str, list[Any]]]:
        """Прематч-события по всем провайдерам: [(provider_name, [Match, ...]), ...]."""
        out: list[tuple[str, list[Any]]] = []
        for provider in self.providers:
            if not provider.available:
                continue
            try:
                matches = await provider.get_upcoming(sport, day)
                out.append((provider.provider_name, matches))
            except SourceError as exc:
                logger.warning("aggregator: {} не отдал прематч {} ({})", provider.provider_name, sport, exc)
            except Exception as exc:  # noqa: BLE001
                logger.exception("aggregator: ошибка {} на прематче {}: {}", provider.provider_name, sport, exc)
        return out

    # ------------------------------------------------------------- persistence
    async def store_snapshots(self, session: AsyncSession, match_id: int, odds: list[Odd]) -> int:
        """Пишет снимки в таблицу odds. Повторные одинаковые цены не дублируем."""
        if not odds:
            return 0
        latest_prices = await self._latest_prices(session, match_id)
        written = 0
        for odd in odds:
            key = (odd.market, odd.selection, odd.line)
            previous = latest_prices.get(key)
            if previous is not None and abs(previous[0] - odd.price) < max(settings.odds_snapshot_min_delta, 1e-9):
                continue  # цена не изменилась — не плодим строки
            session.add(
                OddRow(
                    match_id=match_id,
                    market=odd.market,
                    selection=odd.selection,
                    line=odd.line,
                    price=odd.price,
                    source=odd.source,
                    captured_at=odd.captured_at if odd.captured_at else datetime.now(timezone.utc),
                )
            )
            written += 1
            latest_prices[key] = (odd.price, odd.source)
        await session.flush()
        if written:
            logger.debug("aggregator: match_id={} — записано {} новых снимков кэфов", match_id, written)
        return written

    @staticmethod
    async def _latest_prices(session: AsyncSession, match_id: int) -> dict[tuple[str, str, float | None], tuple[float, str]]:
        rows = (
            await session.execute(
                select(OddRow.market, OddRow.selection, OddRow.line, OddRow.price, OddRow.source)
                .where(OddRow.match_id == match_id)
                .order_by(desc(OddRow.captured_at))
                .limit(500)
            )
        ).all()
        latest: dict[tuple[str, str, float | None], tuple[float, str]] = {}
        for market, selection, line, price, source in rows:
            latest.setdefault((market, selection, line), (price, source))
        return latest

    async def aggregated_from_db(self, session: AsyncSession, match_id: int) -> list[AggregatedOutcome]:
        """Агрегирует последние снимки из БД (используется в PASS 2 и лайве)."""
        latest: dict[tuple[str, str, float | None, str], OddRow] = {}
        rows = (
            await session.execute(
                select(OddRow).where(OddRow.match_id == match_id).order_by(desc(OddRow.captured_at)).limit(800)
            )
        ).scalars().all()
        for row in rows:
            key = (row.market, row.selection, row.line, row.source)
            if key not in latest:
                latest[key] = row
        odds = [
            Odd(
                market=row.market,
                selection=row.selection,
                line=row.line,
                price=row.price,
                source=row.source,
                captured_at=row.captured_at,
            )
            for row in latest.values()
        ]
        return aggregate_outcomes(odds)

    # ----------------------------------------------------------- convenience
    async def refresh_match(self, session: AsyncSession, match: Match, live: bool = False) -> list[AggregatedOutcome]:
        """Полный цикл для матча: собрать с источников, записать снимки, агрегировать."""
        ext_ids = dict(match.external_refs or {})
        if not ext_ids:
            logger.debug("aggregator: у матча {} нет external_refs — только агрегация из БД", match.id)
            return await self.aggregated_from_db(session, match.id)
        collected = await self.collect_for_match(ext_ids, live=live)
        if collected:
            await self.store_snapshots(session, match.id, collected)
        outcomes = await self.aggregated_from_db(session, match.id)
        suspicious = [outcome for outcome in outcomes if outcome.suspicious]
        if suspicious:
            logger.warning(
                "aggregator: match_id={} — {} suspicious-линий: {}",
                match.id, len(suspicious),
                "; ".join(filter(None, (outcome.suspicious_reason for outcome in suspicious)))[:400],
            )
        return outcomes


def find_outcome(
    outcomes: list[AggregatedOutcome], market: str, selection: str, line: float | None = None
) -> AggregatedOutcome | None:
    """Поиск агрегированного исхода (с допуском на float-сравнение линий)."""
    for outcome in outcomes:
        if outcome.market != market or outcome.selection != selection:
            continue
        if line is None and outcome.line is None:
            return outcome
        if line is not None and outcome.line is not None and abs(outcome.line - line) < 1e-6:
            return outcome
    return None


async def odds_line_for(
    session: AsyncSession, match_id: int, market: str, selection: str, line: float | None
) -> float | None:
    """Самый свежий кэф по исходу (для быстрых проверок в лайв-воркере)."""
    row = (
        await session.execute(
            select(OddRow.price)
            .where(
                OddRow.match_id == match_id,
                OddRow.market == market,
                OddRow.selection == selection,
                OddRow.line.is_(None) if line is None else OddRow.line == line,
            )
            .order_by(desc(OddRow.captured_at))
            .limit(1)
        )
    ).scalar_one_or_none()
    return row
