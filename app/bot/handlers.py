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

import html
import time
from collections import defaultdict, deque
from typing import Any

from aiogram import BaseMiddleware, F, Router
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    TelegramObject,
)
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.database import session_scope
from app.db.models import Signal, SignalStatus, User
from app.llm_client import format_llm_stats_text
from app.tracking.results_tracker import performance_by_sport, performance_stats

router = Router(name="betsignals")


def is_admin_user(user_id: int | None) -> bool:
    """Проверка, является ли пользователь администратором."""
    if user_id is None:
        return False
    return user_id in settings.admin_ids


def get_main_keyboard(is_admin: bool = False) -> ReplyKeyboardMarkup:
    """Главная клавиатура бота: выбор команд нажатием кнопок вместо ручного ввода."""
    rows = [
        [KeyboardButton(text="📊 Статистика"), KeyboardButton(text="🎯 Сигналы")],
        [KeyboardButton(text="ℹ️ Справка")],
    ]
    if is_admin:
        rows[1].append(KeyboardButton(text="⚙️ Админка"))
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def get_admin_inline_keyboard() -> InlineKeyboardMarkup:
    """Инлайн-панель управления администратора."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="💰 Расходы LLM", callback_data="admin_costs"),
                InlineKeyboardButton(text="🔍 Статус системы", callback_data="admin_system"),
            ],
            [
                InlineKeyboardButton(text="🔄 Собрать расписание", callback_data="admin_collect_now"),
                InlineKeyboardButton(text="⚡️ Запустить анализ", callback_data="admin_analyze_now"),
            ],
        ]
    )


async def system_status_text() -> str:
    """Текстовая сводка состояния сервиса, БД и источников."""
    from app.db.database import healthcheck
    from app.pipeline.collector import get_providers
    from app.sources.theoddsapi import TheOddsApiProvider

    db_ok = await healthcheck()
    providers = get_providers()
    desc = providers.describe()

    theodds_info = ""
    theodds_prov = getattr(providers, "theoddsapi", None)
    if isinstance(theodds_prov, TheOddsApiProvider):
        quota = theodds_prov.quota_info()
        theodds_info = f"\n   • TheOddsApi: использовано {quota['requests_used_local']}/{quota['monthly_limit']}"

    return (
        "🔍 <b>Статус системы BetSignals</b>\n\n"
        f"• <b>БД PostgreSQL:</b> {'🟢 доступна' if db_ok else '🔴 НЕДОСТУПНА'}\n"
        f"• <b>Часовой пояс:</b> {settings.tz}\n"
        f"• <b>Источники кэфов:</b> {desc.get('odds', {})}{theodds_info}\n"
        f"• <b>Источники статистики:</b> {desc.get('stats', {})}\n\n"
        f"<b>LLM конфигурация:</b>\n"
        f"• Скринер: <code>{settings.screener_model}</code>\n"
        f"• Аналитик: <code>{settings.analyzer_model}</code>\n"
        f"• Арбитр: <code>{settings.judge_model if settings.judge_enabled else 'выключен'}</code>"
    )


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
# Команды и кнопки
# --------------------------------------------------------------------------- #
@router.message(F.text == "/start")
@router.message(F.text.startswith("/start "))
async def cmd_start(message: Message) -> None:
    if message.from_user is None:
        return
    async with session_scope() as session:
        _user, is_new = await register_user(session, message.from_user.id, message.from_user.username)
    logger.info("bot: /start от {} (new={})", message.from_user.id, is_new)
    is_admin = is_admin_user(message.from_user.id)
    text = (
        "👋 Добро пожаловать в <b>BetSignals</b>!\n\n"
        + ("Вы подписаны на сигналы. " if is_new else "Вы снова в списке рассылки. ")
        + "Сигналы будут приходить автоматически.\n\n"
        + f"Ваш Telegram ID: <code>{message.from_user.id}</code>\n"
        + ("(Вы авторизованы как ⭐️ <b>Администратор</b>)\n\n" if is_admin else "")
        + "Для управления используйте кнопки внизу или меню команд [ / ]:\n\n"
        + HELP_TEXT
    )
    await message.answer(text, reply_markup=get_main_keyboard(is_admin))


@router.message(F.text == "/stop")
async def cmd_stop(message: Message) -> None:
    if message.from_user is None:
        return
    updated = await set_active(message.from_user.id, False)
    logger.info("bot: /stop от {} (found={})", message.from_user.id, updated)
    is_admin = is_admin_user(message.from_user.id)
    await message.answer(
        "🔕 Рассылка выключена. Команда /start включит её снова.\n"
        "Историю сигналов можно посмотреть командой /signals.",
        reply_markup=get_main_keyboard(is_admin),
    )


@router.message(F.text.in_({"🎯 Сигналы", "/signals"}))
async def cmd_signals(message: Message) -> None:
    is_admin = is_admin_user(message.from_user.id if message.from_user else None)
    async with session_scope() as session:
        text = await _recent_signals_text(session)
    await message.answer(text, reply_markup=get_main_keyboard(is_admin))


@router.message(F.text.in_({"📊 Статистика", "/stats"}))
async def cmd_stats(message: Message) -> None:
    is_admin = is_admin_user(message.from_user.id if message.from_user else None)
    await message.answer(await stats_text(days=30), reply_markup=get_main_keyboard(is_admin))


@router.message(F.text.in_({"ℹ️ Справка", "/help"}))
async def cmd_help(message: Message) -> None:
    is_admin = is_admin_user(message.from_user.id if message.from_user else None)
    text = HELP_TEXT
    if is_admin:
        text += (
            "\n\n⚙️ <b>Команды администратора:</b>\n"
            "/admin — панель управления\n"
            "/costs — расходы и токены LLM\n"
            "/system — статус источников и БД\n"
            "/collect_now — принудительный сбор расписания\n"
            "/analyze_now — запустить анализ матчей"
        )
    await message.answer(text, reply_markup=get_main_keyboard(is_admin))


# --------------------------------------------------------------------------- #
# Админ-функционал (только для TELEGRAM_ADMIN_IDS)
# --------------------------------------------------------------------------- #
@router.message(F.text.in_({"⚙️ Админка", "/admin"}))
async def cmd_admin(message: Message) -> None:
    if not is_admin_user(message.from_user.id if message.from_user else None):
        await message.answer("⛔️ Доступ запрещён. Команда доступна только администраторам.")
        return
    await message.answer(
        "⚙️ <b>Панель администратора BetSignals</b>\nВыберите действие кнопкой ниже:",
        reply_markup=get_admin_inline_keyboard(),
    )


@router.message(F.text == "/costs")
async def cmd_costs(message: Message) -> None:
    if not is_admin_user(message.from_user.id if message.from_user else None):
        await message.answer("⛔️ Доступ запрещён. Команда доступна только администраторам.")
        return
    await message.answer(format_llm_stats_text())


@router.message(F.text == "/system")
async def cmd_system(message: Message) -> None:
    if not is_admin_user(message.from_user.id if message.from_user else None):
        await message.answer("⛔️ Доступ запрещён. Команда доступна только администраторам.")
        return
    await message.answer(await system_status_text())


async def _execute_collect(message: Message) -> None:
    progress = await message.answer("⏳ <b>Запущен принудительный сбор расписания матчей...</b>")
    try:
        from app.scheduler import job_collect_schedule

        result = await job_collect_schedule()
        details = ", ".join(f"{k}: {v}" for k, v in result.items()) if result else "нет новых матчей"
        await progress.edit_text(f"✅ <b>Сбор расписания завершён!</b>\nРезультат: <code>{details}</code>")
    except Exception as exc:
        logger.exception("admin: ошибка сбора расписания: {}", exc)
        await progress.edit_text(f"❌ <b>Ошибка при сборе расписания:</b>\n<code>{html.escape(str(exc)[:300])}</code>")


async def _execute_analyze(message: Message) -> None:
    progress = await message.answer("⏳ <b>Запущен принудительный анализ матчей (PASS 1 & 2)...</b>")
    try:
        from app.scheduler import job_prematch_passes

        result = await job_prematch_passes()
        await progress.edit_text(f"✅ <b>Анализ завершён!</b>\nРезультат: <code>{result}</code>")
    except Exception as exc:
        logger.exception("admin: ошибка анализа: {}", exc)
        await progress.edit_text(f"❌ <b>Ошибка при анализе:</b>\n<code>{html.escape(str(exc)[:300])}</code>")


@router.message(F.text == "/collect_now")
async def cmd_collect_now(message: Message) -> None:
    if not is_admin_user(message.from_user.id if message.from_user else None):
        await message.answer("⛔️ Доступ запрещён. Команда доступна только администраторам.")
        return
    await _execute_collect(message)


@router.message(F.text == "/analyze_now")
async def cmd_analyze_now(message: Message) -> None:
    if not is_admin_user(message.from_user.id if message.from_user else None):
        await message.answer("⛔️ Доступ запрещён. Команда доступна только администраторам.")
        return
    await _execute_analyze(message)


# --------------------------------------------------------------------------- #
# Инлайн-кнопки админки
# --------------------------------------------------------------------------- #
@router.callback_query(F.data == "admin_menu")
async def callback_admin_menu(callback: CallbackQuery) -> None:
    if not is_admin_user(callback.from_user.id):
        await callback.answer("⛔️ Доступ запрещён", show_alert=True)
        return
    if callback.message and isinstance(callback.message, Message):
        await callback.message.edit_text(
            "⚙️ <b>Панель администратора BetSignals</b>\nВыберите действие кнопкой ниже:",
            reply_markup=get_admin_inline_keyboard(),
        )
    await callback.answer()


@router.callback_query(F.data == "admin_costs")
async def callback_admin_costs(callback: CallbackQuery) -> None:
    if not is_admin_user(callback.from_user.id):
        await callback.answer("⛔️ Доступ запрещён", show_alert=True)
        return
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Обновить", callback_data="admin_costs")],
            [InlineKeyboardButton(text="◀️ В меню", callback_data="admin_menu")],
        ]
    )
    if callback.message and isinstance(callback.message, Message):
        await callback.message.edit_text(format_llm_stats_text(), reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data == "admin_system")
async def callback_admin_system(callback: CallbackQuery) -> None:
    if not is_admin_user(callback.from_user.id):
        await callback.answer("⛔️ Доступ запрещён", show_alert=True)
        return
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Обновить", callback_data="admin_system")],
            [InlineKeyboardButton(text="◀️ В меню", callback_data="admin_menu")],
        ]
    )
    if callback.message and isinstance(callback.message, Message):
        await callback.message.edit_text(await system_status_text(), reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data == "admin_collect_now")
async def callback_admin_collect_now(callback: CallbackQuery) -> None:
    if not is_admin_user(callback.from_user.id):
        await callback.answer("⛔️ Доступ запрещён", show_alert=True)
        return
    await callback.answer("Запускаю сбор расписания...")
    if callback.message and isinstance(callback.message, Message):
        await _execute_collect(callback.message)


@router.callback_query(F.data == "admin_analyze_now")
async def callback_admin_analyze_now(callback: CallbackQuery) -> None:
    if not is_admin_user(callback.from_user.id):
        await callback.answer("⛔️ Доступ запрещён", show_alert=True)
        return
    await callback.answer("Запускаю анализ...")
    if callback.message and isinstance(callback.message, Message):
        await _execute_analyze(callback.message)


@router.message()
async def fallback(message: Message) -> None:
    """Любой другой текст — короткая подсказка (бот не «разговаривает»: LLM занят анализом)."""
    is_admin = is_admin_user(message.from_user.id if message.from_user else None)
    await message.answer(
        "Я понимаю только команды. Нажмите кнопку внизу или выберите команду из меню [ / ].",
        reply_markup=get_main_keyboard(is_admin),
    )
