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
from loguru import logger

from app.bot.handlers import AntifloodMiddleware, router
from app.config import settings


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
