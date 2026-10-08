"""Движение лайв-кэфов (Модуль 7).

В таблице `odds` каждая цена — это снимок с таймстампом (append-only), поэтому
движение линии считается тривиально: берём первое и последнее значение в окне.

Логика из ТЗ, реализованная в `assess_movement`:
  (а) счёт/данные указывают на команду А, а кэф на неё ещё НЕ отыграл падение → возможен value;
  (б) кэф резко упал при нейтральном счёте → рынок что-то увидел → сигнал НЕ создаём,
      матч помечаем is_market_moves=true и логируем для self-review.

Это не «предсказание», а фильтр: он не даёт создавать сигналы там, где линия уже
догнала реальность (главный источник минуса в лайве).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy import asc, desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import Match, Odd as OddRow

VALUE_WINDOW = "value_window"
MARKET_MOVES = "market_moves"
NO_SIGNAL = "no_signal"


@dataclass
class Movement:
    market: str
    selection: str
    line: float | None
    first_price: float
    last_price: float
    first_at: datetime | None
    last_at: datetime | None
    window_minutes: float
    source: str = ""

    @property
    def delta(self) -> float:
        return self.last_price - self.first_price

    @property
    def delta_pct(self) -> float:
        if self.first_price <= 0:
            return 0.0
        return self.delta / self.first_price

    @property
    def dropped(self) -> bool:
        """Кэф упал (рынок «поверил» в исход)."""
        return self.delta <= -settings.odds_movement_min_delta

    @property
    def rose(self) -> bool:
        return self.delta >= settings.odds_movement_min_delta

    def as_dict(self) -> dict[str, float | str | None]:
        return {
            "market": self.market,
            "selection": self.selection,
            "line": self.line,
            "first_price": round(self.first_price, 3),
            "last_price": round(self.last_price, 3),
            "delta": round(self.delta, 3),
            "delta_pct": round(self.delta_pct, 4),
            "window_minutes": round(self.window_minutes, 1),
            "source": self.source,
        }


async def compute_movement(
    session: AsyncSession,
    match_id: int,
    market: str,
    selection: str,
    line: float | None,
    window_minutes: int | None = None,
) -> Movement | None:
    """Считает дельту кэфа за окно (по умолчанию 10 минут)."""
    window_minutes = window_minutes or settings.odds_movement_window_min
    since = datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
    base_query = select(OddRow).where(
        OddRow.match_id == match_id,
        OddRow.market == market,
        OddRow.selection == selection,
        OddRow.captured_at >= since,
        OddRow.line.is_(None) if line is None else OddRow.line == line,
    )
    first = (await session.execute(base_query.order_by(asc(OddRow.captured_at)).limit(1))).scalar_one_or_none()
    last = (await session.execute(base_query.order_by(desc(OddRow.captured_at)).limit(1))).scalar_one_or_none()
    if first is None or last is None or first.id == last.id:
        return None
    span = (last.captured_at - first.captured_at).total_seconds() / 60.0 if first.captured_at and last.captured_at else 0.0
    return Movement(
        market=market,
        selection=selection,
        line=line,
        first_price=first.price,
        last_price=last.price,
        first_at=first.captured_at,
        last_at=last.captured_at,
        window_minutes=span,
        source=last.source,
    )


async def movement_report(
    session: AsyncSession, match_id: int, window_minutes: int | None = None, limit: int = 12
) -> list[dict]:
    """Движение по всем исходам матча (для промпта арбитра и логов)."""
    window_minutes = window_minutes or settings.odds_movement_window_min
    since = datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
    rows = (
        await session.execute(
            select(OddRow.market, OddRow.selection, OddRow.line)
            .where(OddRow.match_id == match_id, OddRow.captured_at >= since)
            .distinct()
            .limit(limit * 3)
        )
    ).all()
    report: list[dict] = []
    for market, selection, line in rows:
        movement = await compute_movement(session, match_id, market, selection, line, window_minutes)
        if movement is not None:
            report.append(movement.as_dict())
    report.sort(key=lambda item: abs(float(item["delta_pct"])), reverse=True)
    return report[:limit]


def assess_movement(
    movement: Movement | None,
    *,
    signal_selection: str,
    leading_team_selection: str | None,
    score_margin_abs: float | None,
) -> tuple[str, str]:
    """Классифицирует ситуацию по движению кэфа.

    Возвращает (verdict, reason):
      * market_moves — кэф резко упал при нейтральном счёте → сигнал не создаём;
      * value_window — данные указывают на исход, а кэф ещё не отыграл;
      * no_signal — движение не даёт преимущества.
    """
    if movement is None:
        return NO_SIGNAL, "движения кэфа в окне нет"

    neutral = (
        score_margin_abs is not None
        and score_margin_abs <= settings.odds_movement_neutral_threshold
    )
    if movement.dropped and neutral:
        return (
            MARKET_MOVES,
            f"кэф упал {movement.delta:+.2f} ({movement.delta_pct:+.1%}) при нейтральном счёте — "
            "рынок что-то увидел, сигнал не создаём",
        )
    if leading_team_selection and leading_team_selection == signal_selection and not movement.dropped:
        return (
            VALUE_WINDOW,
            f"данные указывают на {signal_selection}, кэф не упал ({movement.delta:+.2f}) — окно value",
        )
    if movement.dropped:
        return NO_SIGNAL, f"кэф уже отыграл падение ({movement.delta:+.2f}) — окна нет"
    return NO_SIGNAL, f"движение {movement.delta:+.2f} не даёт преимущества"


async def flag_market_moves(session: AsyncSession, match: Match, reason: str) -> None:
    """Помечает матч как market_moves и логирует (для self-review)."""
    if not match.is_market_moves:
        match.is_market_moves = True
        await session.flush()
    logger.info("odds_movement: match_id={} помечен market_moves — {}", match.id, reason)
