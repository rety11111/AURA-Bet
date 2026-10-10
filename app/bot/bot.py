"""Telegram-бот (Модуль 1 ТЗ «боевая часть»): фабрика Bot/Dispatcher.

Бот отвечает только на команды (подписок/меню нет — по ТЗ это минималистичный бот):
  /start  — подписаться на сигналы (пользователь появляется в `users` с is_active=True)
  /stop   — отписаться (is_active=False), сигналы больше не приходят
  /signals— последние 10 сигналов (любой статус) с результатами
  /stats  — статистика: винрейт и ROI за 7/30 дней, по спорту
  /help   — справка

Отправку сигналов делает app/bot/broadcaster.py; логика команд — handlers.py.
"""

from __future__ import annotations

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault
from loguru import logger

from app.bot.handlers import AntifloodMiddleware, router
from app.config import settings


async def setup_bot_commands(bot: Bot) -> None:
    """Устанавливает официальное меню команд Telegram (кнопка [/] в интерфейсе)."""
    user_commands = [
        BotCommand(command="start", description="🚀 Подписаться / перезапуск"),
        BotCommand(command="stats", description="📊 Статистика: ROI и винрейт"),
        BotCommand(command="signals", description="🎯 Последние сигналы"),
        BotCommand(command="help", description="ℹ️ Справка и описание"),
        BotCommand(command="stop", description="🔕 Отключить рассылку"),
    ]
    try:
        await bot.set_my_commands(user_commands, scope=BotCommandScopeDefault())
        for admin_id in settings.admin_ids:
            admin_commands = user_commands + [
                BotCommand(command="admin", description="⚙️ Панель администратора"),
                BotCommand(command="costs", description="💰 Расходы и токены LLM"),
                BotCommand(command="system", description="🔍 Статус сервиса"),
                BotCommand(command="collect_now", description="🔄 Собрать матчи сейчас"),
                BotCommand(command="analyze_now", description="⚡️ Запустить анализ сейчас"),
            ]
            try:
                await bot.set_my_commands(admin_commands, scope=BotCommandScopeChat(chat_id=admin_id))
            except Exception as exc:
                logger.debug("bot: set_my_commands для админа {} ({})", admin_id, exc)
    except Exception as exc:
        logger.warning("bot: не удалось зарегистрировать меню команд ({})", exc)


def create_bot() -> Bot:
    """Bot с HTML-парсингом по умолчанию (в сообщениях используются <b>, <i>, <code>)."""
    if not settings.telegram_bot_token:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN не задан. Получите токен у @BotFather и пропишите в .env (см. SETUP.md)."
        )
    bot = Bot(
        token=settings.telegram_bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    logger.info("bot: создан (токен …{})", settings.telegram_bot_token[-6:])
    return bot


def create_dispatcher() -> Dispatcher:
    """Dispatcher с роутером команд и антифлудом."""
    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher.update.outer_middleware(AntifloodMiddleware())
    dispatcher.include_router(router)
    logger.info("bot: dispatcher собран (команды /start /stop /signals /stats /help)")
    return dispatcher


async def notify_admins(bot: Bot, text: str) -> int:
    """Отправляет служебное сообщение админам (TELEGRAM_ADMIN_IDS). Возвращает число доставок."""
    delivered = 0
    for admin_id in settings.admin_ids:
        try:
            await bot.send_message(admin_id, text[:4000])
            delivered += 1
        except Exception as exc:
            logger.warning("bot: не удалось написать админу {} ({})", admin_id, exc)
    return delivered
