"""Обработчики команд бота + антифлуд (Модуль 1 ТЗ).

Команды:
  /start   — регистрирует пользователя в `users` и включает рассылку;
  /stop    — выключает рассылку (is_active=False), история сигналов остаётся;
  /signals — последние 10 сигналов с человекочитаемым статусом;
  /stats   — винрейт/ROI за 7 и 30 дней + разрез по спорту;
  /help    — справка.

Антифлуд: не чаще 1 сообщения в секунду и не более 12 команд в минуту с одного
пользователя. Первое превышение — предупреждение, дальше молча игнорируем
(чтобы спамер не мог «размножить» ответы бота).
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from typing import Any

from aiogram import BaseMiddleware, F, Router
from aiogram.types import Message, TelegramObject
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.database import session_scope
from app.db.models import Signal, SignalStatus, User
from app.tracking.results_tracker import performance_by_sport, performance_stats

router = Router(name="betsignals")

HELP_TEXT = (
    "<b>BetSignals</b> — сигналы value-ставок, отобранные связкой LLM + стат-моделей.\n\n"
    "Команды:\n"
    "/start — подписаться на сигналы\n"
    "/stop — отписаться\n"
    "/signals — последние 10 сигналов\n"
    "/stats — статистика (винрейт, ROI)\n"
    "/help — эта справка\n\n"
    "Сигналы приходят автоматически: прематч — после двух проходов анализа и арбитра "
    "(Claude), лайв — по киберспорту (Dota 2 / CS2) в узкие окна.\n"
    "Ставки — это риск. Сервис не гарантирует прибыль, размер ставки указан в % от банка "
    "и рассчитан дробным Келли с кэпом."
)

STATUS_EMOJI = {
    SignalStatus.CANDIDATE: "🕓",
    SignalStatus.CONFIRMED: "✅",
    SignalStatus.SENT: "📨",
    SignalStatus.WON: "🟢",
    SignalStatus.LOST: "🔴",
    SignalStatus.VOID: "⚪",
    SignalStatus.REJECTED: "⛔",
}

SPORT_EMOJI = {
    "football": "⚽",
    "hockey": "🏒",
    "basketball": "🏀",
    "tennis": "🎾",
    "mma": "🥊",
    "boxing": "🥊",
    "dota2": "🎮",
    "cs2": "🔫",
}


class AntifloodMiddleware(BaseMiddleware):
    """Ограничивает частоту команд: 1/сек и 12/мин на пользователя."""

    def __init__(self, per_second: float = 1.0, per_minute: int = 12) -> None:
        self.per_second = per_second
        self.per_minute = per_minute
        self._last_call: dict[int, float] = {}
        self._minute_window: dict[int, deque[float]] = defaultdict(deque)
        self._warned: set[int] = set()

    async def __call__(self, handler: Any, event: TelegramObject, data: dict[str, Any]) -> Any:
        message = event if isinstance(event, Message) else getattr(event, "message", None)
        user = getattr(message, "from_user", None)
        if user is None:
            return await handler(event, data)

        now = time.monotonic()
        last = self._last_call.get(user.id)
        window = self._minute_window[user.id]
        while window and now - window[0] > 60:
            window.popleft()

        too_fast = last is not None and (now - last) < self.per_second
        too_many = len(window) >= self.per_minute
        self._last_call[user.id] = now
        window.append(now)

        if too_fast or too_many:
            if user.id not in self._warned:
                self._warned.add(user.id)
                try:
                    await message.answer("⏳ Слишком часто. Подождите секунду — команды не спешат.")
                except Exception as exc:  # бот мог быть заблокирован пользователем
                    logger.debug("antiflood: не удалось ответить {} ({})", user.id, exc)
            logger.debug("antiflood: {} — {} (fast={}, many={})", user.id, "отклонено", too_fast, too_many)
            return None
        self._warned.discard(user.id)
        return await handler(event, data)


# --------------------------------------------------------------------------- #
# Работа с пользователями
# --------------------------------------------------------------------------- #
async def register_user(session: AsyncSession, tg_id: int, username: str | None) -> tuple[User, bool]:
    """Создаёт/обновляет пользователя. Возвращает (user, is_new)."""
    user = (await session.execute(select(User).where(User.tg_id == tg_id))).scalar_one_or_none()
    is_new = user is None
    if user is None:
        user = User(tg_id=tg_id, username=username, is_active=True)
        session.add(user)
    else:
        user.username = username or user.username
        user.is_active = True
    await session.commit()
    return user, is_new


async def set_active(tg_id: int, active: bool) -> bool:
    async with session_scope() as session:
        user = (await session.execute(select(User).where(User.tg_id == tg_id))).scalar_one_or_none()
        if user is None:
            return False
        user.is_active = active
        await session.commit()
    return True


async def active_user_ids(session: AsyncSession) -> list[int]:
    rows = (await session.execute(select(User.tg_id).where(User.is_active.is_(True)))).scalars().all()
    return [int(tg_id) for tg_id in rows]


# --------------------------------------------------------------------------- #
# Форматирование
# --------------------------------------------------------------------------- #
def _fmt_signal_line(signal: Signal, match_label: str, sport_code: str | None) -> str:
    emoji = STATUS_EMOJI.get(signal.status, "•")
    sport_icon = SPORT_EMOJI.get(sport_code or "", "🏆")
    live_mark = " 🔴LIVE" if signal.is_live else ""
    selection = f"{signal.market} {signal.selection}" + (f" {signal.line:g}" if signal.line is not None else "")
    return (
        f"{emoji} {sport_icon} {match_label}{live_mark}\n"
        f"   <code>{selection}</code> @ {signal.odds:.2f} · "
        f"edge {signal.edge:+.1%} · уверенность {signal.confidence_score:.0f}/100 · ставка {signal.stake_pct:.2f}%"
    )


async def _recent_signals_text(session: AsyncSession, limit: int = 10) -> str:
    from app.db.models import Match as MatchRow, Sport, Team

    rows = (
        await session.execute(
            select(Signal, MatchRow, Sport.code, Team.canonical_name)
            .join(MatchRow, MatchRow.id == Signal.match_id)
            .join(Sport, Sport.id == MatchRow.sport_id)
            .outerjoin(Team, Team.id == MatchRow.home_team_id)
            .order_by(Signal.created_at.desc())
            .limit(limit)
        )
    ).all()
    if not rows:
        return "Пока сигналов нет. Первые появятся после прохода анализа (см. /help)."

    lines = ["<b>Последние сигналы</b>"]
    home_names = {row[1].home_team_id: row[3] for row in rows}
    for signal, match, sport_code, _home_name in rows:
        home = home_names.get(match.home_team_id) or "дом"
        lines.append(_fmt_signal_line(signal, f"{home} — <i>гости</i> (матч {match.id})", sport_code))
    lines.append("\nСтатусы: 🟢 выиграл · 🔴 проиграл · ⚪ возврат · 📨 отправлен · ✅ подтверждён")
    return "\n".join(lines)


async def stats_text(days: int = 30) -> str:
    async with session_scope() as session:
        overall = await performance_stats(session, days=days)
        by_sport = await performance_by_sport(session, days=days)

    lines = [
        f"<b>Статистика за {days} дней</b>",
        f"Сигналов: {overall['signals']} (🟢 {overall['won']} · 🔴 {overall['lost']} · ⚪ {overall['void']}, "
        f"открытых {overall['open']})",
        f"Винрейт: {overall['win_rate']:.1%} · ROI: {overall['roi']:+.1%} · профит: {overall['profit_units']:+.2f}% банка",
        f"Средний кэф: {overall['avg_odds']:.2f} · средний edge: {overall['avg_edge']:+.1%} · "
        f"средняя уверенность: {overall['avg_score']:.0f}/100",
    ]
    interesting = [
        (sport, payload) for sport, payload in by_sport.items() if payload["signals"] > 0
    ]
    if interesting:
        lines.append("\n<b>По видам спорта</b>")
        for sport, payload in sorted(interesting, key=lambda item: item[1]["roi"], reverse=True):
            icon = SPORT_EMOJI.get(sport, "🏆")
            lines.append(
                f"{icon} {sport}: {payload['signals']} сигн., винрейт {payload['win_rate']:.1%}, ROI {payload['roi']:+.1%}"
            )
    lines.append("\nROI считается в единицах ставки (доля банка), возвраты (void) в знаменатель не входят.")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Команды
# --------------------------------------------------------------------------- #
@router.message(F.text == "/start")
@router.message(F.text.startswith("/start "))
async def cmd_start(message: Message) -> None:
    if message.from_user is None:
        return
    async with session_scope() as session:
        _user, is_new = await register_user(session, message.from_user.id, message.from_user.username)
    logger.info("bot: /start от {} (new={})", message.from_user.id, is_new)
    text = (
        "👋 Добро пожаловать в <b>BetSignals</b>!\n\n"
        + ("Вы подписаны на сигналы. " if is_new else "Вы снова в списке рассылки. ")
        + "Сигналы будут приходить автоматически.\n\n"
        + f"Ваш Telegram ID: <code>{message.from_user.id}</code>\n"
        + "(нужен для переменной TELEGRAM_ADMIN_IDS — см. SETUP.md)\n\n"
        + HELP_TEXT
    )
    await message.answer(text)


@router.message(F.text == "/stop")
async def cmd_stop(message: Message) -> None:
    if message.from_user is None:
        return
    updated = await set_active(message.from_user.id, False)
    logger.info("bot: /stop от {} (found={})", message.from_user.id, updated)
    await message.answer(
        "🔕 Рассылка выключена. Команда /start включит её снова.\n"
        "Историю сигналов можно посмотреть командой /signals."
    )


@router.message(F.text == "/signals")
async def cmd_signals(message: Message) -> None:
    async with session_scope() as session:
        text = await _recent_signals_text(session)
    await message.answer(text)


@router.message(F.text == "/stats")
async def cmd_stats(message: Message) -> None:
    await message.answer(await stats_text(days=30))


@router.message(F.text == "/help")
async def cmd_help(message: Message) -> None:
    await message.answer(HELP_TEXT)


@router.message()
async def fallback(message: Message) -> None:
    """Любой другой текст — короткая подсказка (бот не «разговаривает»: LLM занят анализом)."""
    await message.answer("Я понимаю только команды. Наберите /help — там список.")
