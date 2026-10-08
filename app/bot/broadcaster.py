"""Рассылка сигналов в Telegram (Модуль 1 ТЗ).

Что здесь есть:
  * format_signal_message() — ровно тот шаблон сообщения, что в ТЗ (HTML + эмодзи),
    с пометкой «гипотеза» для сигналов без арбитра живого матча;
  * broadcast_signal() — батчевая отправка всем активным пользователям с
    ограничением скорости (tg_rate_per_sec), параллелизмом (tg_parallelism),
    ретраями (tg_send_retries) и обработкой TelegramRetryAfter;
  * broadcast_pending() — вызывается планировщиком каждую минуту: рассылает всё,
    что подтверждено арбитром (или прошло без него в лайве) и ещё не отправлено;
  * send_daily_digest() — сводка дня в 23:59 МСК;
  * user_deliveries — журнал «кому какой сигнал доставлен» (идемпотентность
    рассылки: повторный запуск не отправит сигнал дважды одному пользователю).

Пользователь, заблокировавший бота, автоматически переводится в is_active=False
(TelegramForbiddenError), чтобы не тратить лимиты.
"""

from __future__ import annotations

import asyncio
import html
from datetime import datetime, timedelta, timezone
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.database import session_scope
from app.db.models import (
    Match as MatchRow,
    Signal,
    SignalStatus,
    Sport,
    Team,
    User,
    UserDelivery,
)
from app.pipeline.value_engine import format_selection, market_label
from app.tracking.results_tracker import performance_by_sport, performance_stats

SPORT_ICONS = {
    "football": "⚽",
    "hockey": "🏒",
    "basketball": "🏀",
    "tennis": "🎾",
    "mma": "🥊",
    "boxing": "🥊",
    "dota2": "🎮",
    "cs2": "🔫",
}
SPORT_LABELS = {
    "football": "Футбол",
    "hockey": "Хоккей",
    "basketball": "Баскетбол",
    "tennis": "Теннис",
    "mma": "MMA",
    "boxing": "Бокс",
    "dota2": "Dota 2",
    "cs2": "CS2",
}


def _msk(moment: datetime | None) -> str:
    if moment is None:
        return "—"
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(settings.tzinfo).strftime("%H:%M МСК %d.%m")


def _human_delta(moment: datetime | None) -> str:
    if moment is None:
        return ""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    delta = moment - datetime.now(timezone.utc)
    total_minutes = int(delta.total_seconds() // 60)
    if total_minutes < 0:
        return "матч идёт"
    if total_minutes < 60:
        return f"через {total_minutes} мин"
    hours, minutes = divmod(total_minutes, 60)
    if hours < 24:
        return f"через {hours}ч {minutes:02d}м"
    return f"через {hours // 24} дн."


def _esc(value: Any, limit: int = 600) -> str:
    return html.escape(str(value if value is not None else ""))[:limit]


async def format_signal_message(
    signal: Signal,
    match: MatchRow,
    sport_code: str,
    home_team: str,
    away_team: str,
) -> str:
    """Собирает текст сообщения строго по шаблону ТЗ (HTML, экранированный)."""
    icon = SPORT_ICONS.get(sport_code, "🏆")
    sport_label = SPORT_LABELS.get(sport_code, sport_code)
    live_mark = f" · 🔴 LIVE{(' (' + _esc(signal.live_stage, 60) + ')') if signal.live_stage else ''}" if signal.is_live else ""

    selection = format_selection(signal.market, signal.selection, signal.line)
    market = market_label(signal.market)
    source = signal.odds_source or "лучшая цена"
    judge_line = _judge_block(signal)

    factors = [str(item) for item in (signal.key_factors or [])][:3]
    risks = [str(item) for item in (signal.risk_notes or [])][:3]

    lines = [
        f"{icon} <b>{_esc(sport_label)}</b> | {_esc(match.league, 120)}{live_mark}",
        f"⚔️ <b>{_esc(home_team)} — {_esc(away_team)}</b>",
        f"⏰ Старт: {_msk(match.starts_at)} ({_human_delta(match.starts_at)})",
        "",
        f"📈 <b>Ставка: {_esc(market)} — {_esc(selection)}</b>",
        f"💰 Кэф: <b>{signal.odds:.2f}</b> ({_esc(source)}) | Value: <b>{signal.edge:+.1%}</b> | Уверенность: <b>{signal.confidence_score:.0f}/100</b>",
        f"💵 Размер: <b>{signal.stake_pct:.2f}%</b> банка (дробный Келли)",
    ]
    if factors:
        lines.append("")
        lines.append("🎯 <b>Почему:</b>")
        lines.extend(f"• {_esc(factor, 220)}" for factor in factors)
    if risks:
        lines.append(f"⚠️ Риски: {_esc('; '.join(risks), 400)}")
    lines.append(judge_line)
    if signal.reasoning:
        lines.append(f"🧠 Обоснование: {_esc(signal.reasoning, 500)}")

    tags = [f"#{sport_label.replace(' ', '')}"]
    if match.league:
        tags.append("#" + _esc(match.league, 40).replace(" ", "").replace("-", ""))
    lines.append("")
    lines.append(" ".join(tags))
    return "\n".join(lines)


def _judge_block(signal: Signal) -> str:
    if signal.judge_reason:
        return f"🛡 Арбитр: <b>проверено</b> — {_esc(signal.judge_reason, 400)}"
    if signal.is_live:
        return "🛡 Арбитр: <b>лайв без арбитра</b> (решение принято за отведённый таймаут)"
    return "🛡 Арбитр: не проверялся (JUDGE_ENABLED=false)"


# Результаты доставки: отличаем «пользователь заблокировал бота» от «сетевого сбоя»,
# чтобы не отключать живых пользователей из-за разового таймаута.
DELIVERY_OK = "ok"
DELIVERY_BLOCKED = "blocked"
DELIVERY_FAILED = "failed"


async def _send_one(bot: Bot, tg_id: int, text: str, semaphore: asyncio.Semaphore) -> str:
    """Отправка одному пользователю с ретраями. Возвращает DELIVERY_*."""
    async with semaphore:
        for attempt in range(1, settings.tg_send_retries + 1):
            try:
                await bot.send_message(tg_id, text, disable_web_page_preview=True)
                return DELIVERY_OK
            except TelegramRetryAfter as exc:
                logger.warning("broadcaster: Telegram просит подождать {}s (user {})", exc.retry_after, tg_id)
                await asyncio.sleep(exc.retry_after + 1)
            except TelegramForbiddenError:
                logger.info("broadcaster: пользователь {} заблокировал бота — отключаю рассылку", tg_id)
                return DELIVERY_BLOCKED
            except Exception as exc:
                logger.warning("broadcaster: сбой отправки {} (попытка {}/{}): {}", tg_id, attempt, settings.tg_send_retries, str(exc)[:160])
                await asyncio.sleep(min(2**attempt, 8))
    return DELIVERY_FAILED


async def _delivery_targets(session: AsyncSession, signal_id: int) -> list[User]:
    """Активные пользователи, которым этот сигнал ещё не доставлялся."""
    delivered = select(UserDelivery.user_id).where(UserDelivery.signal_id == signal_id)
    rows = (
        await session.execute(select(User).where(User.is_active.is_(True), User.id.not_in(delivered)))
    ).scalars().all()
    return list(rows)


async def broadcast_signal(bot: Bot, session: AsyncSession, signal: Signal) -> int:
    """Рассылает один сигнал. Возвращает число доставок."""
    sport_code = (
        await session.execute(
            select(Sport.code)
            .join(MatchRow, MatchRow.sport_id == Sport.id)
            .where(MatchRow.id == signal.match_id)
        )
    ).scalar_one_or_none() or "unknown"
    match = (await session.execute(select(MatchRow).where(MatchRow.id == signal.match_id))).scalar_one()
    team_ids = [team_id for team_id in (match.home_team_id, match.away_team_id) if team_id]
    names = dict(
        (await session.execute(select(Team.id, Team.canonical_name).where(Team.id.in_(team_ids)))).all()
    ) if team_ids else {}
    text = await format_signal_message(
        signal, match, sport_code, names.get(match.home_team_id, "Хозяева"), names.get(match.away_team_id, "Гости")
    )

    users = await _delivery_targets(session, signal.id)
    if not users:
        logger.info("broadcaster: signal_id={} — нет получателей (все уже получили или нет активных)", signal.id)
        signal.status = SignalStatus.SENT
        signal.sent_at = datetime.now(timezone.utc)
        await session.commit()
        return 0

    semaphore = asyncio.Semaphore(max(1, settings.tg_parallelism))
    delivered = 0
    blocked: list[int] = []
    for start in range(0, len(users), settings.tg_batch_size):
        batch = users[start : start + settings.tg_batch_size]
        results = await asyncio.gather(*(_send_one(bot, user.tg_id, text, semaphore) for user in batch))
        for user, status in zip(batch, results):
            if status == DELIVERY_OK:
                delivered += 1
                session.add(UserDelivery(signal_id=signal.id, user_id=user.id))
            elif status == DELIVERY_BLOCKED:
                blocked.append(user.tg_id)
        await session.flush()
        await asyncio.sleep(settings.tg_batch_pause_sec)

    if blocked:
        rows = (await session.execute(select(User).where(User.tg_id.in_(blocked)))).scalars().all()
        for row in rows:
            row.is_active = False
        logger.info("broadcaster: отключена рассылка для {} пользователей (заблокировали бота)", len(rows))

    signal.status = SignalStatus.SENT
    signal.sent_at = datetime.now(timezone.utc)
    await session.commit()
    logger.info(
        "broadcaster: signal_id={} (match_id={}) доставлен {}/{} пользователям",
        signal.id, signal.match_id, delivered, len(users),
    )
    return delivered


async def broadcast_pending(bot: Bot, limit: int = 20) -> int:
    """Рассылает все подтверждённые сигналы, которые ещё не отправлены. Вызывается из scheduler."""
    sent_total = 0
    async with session_scope() as session:
        signals = (
            (
                await session.execute(
                    select(Signal)
                    .where(Signal.status == SignalStatus.CONFIRMED)
                    .order_by(Signal.is_live.desc(), Signal.created_at.asc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        if not signals:
            return 0
        for signal in signals:
            try:
                sent_total += await broadcast_signal(bot, session, signal)
            except Exception as exc:
                logger.error("broadcaster: сигнал {} не разослан ({})", signal.id, str(exc)[:300])
                await session.rollback()
    return sent_total


async def send_daily_digest(bot: Bot) -> int:
    """Сводка дня (23:59 МСК): сколько сигналов, результат, ROI, лучший/худший спорт."""
    async with session_scope() as session:
        today = await performance_stats(session, days=1)
        week = await performance_stats(session, days=7)
        by_sport = await performance_by_sport(session, days=7)

        lines = [
            "📊 <b>BetSignals — итоги дня</b>",
            f"Сегодня: {today['signals']} сигналов (🟢 {today['won']} · 🔴 {today['lost']} · ⚪ {today['void']}), "
            f"ROI {today['roi']:+.1%}",
            f"Неделя: {week['signals']} сигналов, винрейт {week['win_rate']:.1%}, ROI {week['roi']:+.1%}",
        ]
        interesting = [(sport, payload) for sport, payload in by_sport.items() if payload["signals"] >= 3]
        if interesting:
            best = max(interesting, key=lambda item: item[1]["roi"])
            worst = min(interesting, key=lambda item: item[1]["roi"])
            lines.append(
                f"Лучший спорт недели: {SPORT_ICONS.get(best[0], '🏆')} {best[0]} (ROI {best[1]['roi']:+.1%}), "
                f"худший: {SPORT_ICONS.get(worst[0], '🏆')} {worst[0]} (ROI {worst[1]['roi']:+.1%})"
            )
        open_signals = await performance_stats(session, days=30)
        lines.append(f"Открытых сигналов сейчас: {open_signals['open']}")
        lines.append("\nПолная статистика — /stats. Завтра новый сбор с 06:00 МСК.")

        users = (await session.execute(select(User).where(User.is_active.is_(True)))).scalars().all()

    text = "\n".join(lines)
    semaphore = asyncio.Semaphore(max(1, settings.tg_parallelism))
    delivered = 0
    for start in range(0, len(users), settings.tg_batch_size):
        batch = users[start : start + settings.tg_batch_size]
        results = await asyncio.gather(*(_send_one(bot, user.tg_id, text, semaphore) for user in batch))
        delivered += sum(1 for status in results if status == DELIVERY_OK)
        await asyncio.sleep(settings.tg_batch_pause_sec)
    logger.info("broadcaster: дневная сводка доставлена {}/{}", delivered, len(users))
    return delivered


async def broadcast_test_message(bot: Bot, text: str | None = None) -> int:
    """Тестовое сообщение (используется в scripts/check_sources.py и после деплоя)."""
    async with session_scope() as session:
        users = (await session.execute(select(User).where(User.is_active.is_(True)))).scalars().all()
    body = text or (
        "🔔 <b>BetSignals на связи.</b>\n"
        "Это тестовое сообщение: сервис запущен и рассылка работает.\n"
        "Реальные сигналы придут после анализа матчей."
    )
    semaphore = asyncio.Semaphore(max(1, settings.tg_parallelism))
    delivered = 0
    for start in range(0, len(users), settings.tg_batch_size):
        batch = users[start : start + settings.tg_batch_size]
        results = await asyncio.gather(*(_send_one(bot, user.tg_id, body, semaphore) for user in batch))
        delivered += sum(1 for status in results if status == DELIVERY_OK)
        await asyncio.sleep(settings.tg_batch_pause_sec)
    return delivered


async def pending_count() -> int:
    """Сколько подтверждённых сигналов ждёт рассылки — для /health и логов."""
    async with session_scope() as session:
        from sqlalchemy import func

        return int(
            (
                await session.execute(
                    select(func.count(Signal.id)).where(Signal.status == SignalStatus.CONFIRMED)
                )
            ).scalar()
            or 0
        )
