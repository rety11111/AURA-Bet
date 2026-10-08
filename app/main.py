"""Точка входа сервиса: `python -m app.main` (см. Procfile).

Что делает процесс (Railway: web-сервис, НЕ cron):
  1. Настраивает loguru (в т.ч. отдельный файл с ошибками и перехват стандартного logging).
  2. Проверяет preflight: БД доступна, миграции применены, ключи на месте, бот отвечает.
  3. Поднимает healthcheck-сервер на $PORT (Railway ждёт ответа /health).
  4. Создаёт единый контейнер провайдеров, планировщик APScheduler и бота.
  5. Запускает long-polling Telegram и живёт до SIGTERM/SIGINT, затем корректно
     останавливает polling, планировщик, закрывает HTTP-клиенты, LLM и БД.

Все тяжёлые вещи (сбор, анализ, рассылка, обучение) живут в scheduler.py —
здесь только «жизненный цикл» процесса.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from typing import Any

from aiohttp import web
from loguru import logger
from sqlalchemy import text

from app.bot.bot import create_bot, create_dispatcher, notify_admins
from app.config import settings
from app.db.database import (
    dispose_engine,
    ensure_sports,
    get_session_factory,
    healthcheck,
    init_engine,
)
from app.pipeline.collector import get_providers, shutdown_providers
from app.scheduler import create_scheduler, list_jobs


# --------------------------------------------------------------------------- #
# Логирование
# --------------------------------------------------------------------------- #
class InterceptHandler(logging.Handler):
    """Перенаправляет стандартный logging (httpx, aiogram, apscheduler, uvicorn) в loguru.

    Наследуемся от logging.Handler — именно этого требует logging.basicConfig
    (иначе старт падает с AttributeError: 'InterceptHandler' object has no attribute 'formatter').
    """

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level: str | int = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        # Поднимаемся по стеку до первого «не logging» кадра — чтобы в логе был
        # реальный источник записи, а не внутренности logging.
        frame, depth = logging.currentframe(), 2
        while frame is not None and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1
        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


def setup_logging() -> None:
    """loguru: консоль (Railway собирает stdout) + файл ошибок + перехват logging."""
    logger.remove()
    if settings.log_json:
        # LOG_JSON=true — структурированные логи (удобно парсить на Railway).
        logger.add(
            sys.stdout,
            level=settings.log_level,
            serialize=True,
            enqueue=False,
            backtrace=False,
            diagnose=False,
        )
    else:
        logger.add(
            sys.stdout,
            level=settings.log_level,
            format=(
                "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | "
                "<cyan>{name}</cyan>:<cyan>{line}</cyan> | "
                "match_id={extra[match_id]} | <level>{message}</level>"
            ),
            filter=_match_id_patcher,
            enqueue=False,
            backtrace=False,
            diagnose=False,
        )
    logger.add(
        "logs/errors.log",
        level="ERROR",
        rotation="10 MB",
        retention="14 days",
        compression="zip",
        enqueue=True,
        backtrace=True,
        diagnose=False,
    )

    logging.basicConfig(handlers=[InterceptHandler()], level=0, force=True)
    for name in ("httpx", "httpcore", "openai", "asyncio"):
        logging.getLogger(name).setLevel(logging.WARNING)
    logging.getLogger("aiogram").setLevel(logging.INFO)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)


def _match_id_patcher(record: dict[str, Any]) -> bool:
    """Вставляет match_id="-" в extra, если обработчик не привязал его через logger.bind()."""
    record["extra"].setdefault("match_id", record["extra"].get("match_id", "-"))
    return True


# --------------------------------------------------------------------------- #
# Healthcheck
# --------------------------------------------------------------------------- #
async def start_health_server(bot: Any | None = None) -> web.AppRunner:
    """/health для Railway: статус БД, LLM, бота и источников."""
    providers = get_providers()
    bot_info: dict[str, Any] = {"configured": settings.telegram_bot_token != "", "username": None}

    async def health(_request: web.Request) -> web.Response:
        db_ok = await healthcheck()
        payload = {
            "service": settings.app_name,
            "status": "ok" if db_ok else "degraded",
            "db": db_ok,
            "llm_configured": bool(settings.openrouter_api_key),
            "models": {
                "screener": settings.screener_model,
                "analyzer": settings.analyzer_model,
                "judge": settings.judge_model if settings.judge_enabled else "disabled",
            },
            "bot": bot_info,
            "providers": providers.describe(),
        }
        return web.json_response(payload, status=200 if db_ok else 503)

    async def root(_request: web.Request) -> web.Response:
        return web.json_response({"service": settings.app_name, "health": "/health", "tz": settings.tz})

    app = web.Application()
    app.router.add_get("/health", health)
    app.router.add_get("/", root)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", settings.healthcheck_port))
    site = web.TCPSite(runner, host="0.0.0.0", port=port)
    await site.start()
    logger.info("health: сервер слушает http://0.0.0.0:{}/health", port)

    if bot is not None:
        try:
            me = await bot.get_me()
            bot_info["username"] = me.username
            bot_info["configured"] = True
            logger.info("health: бот @{} на связи", me.username)
        except Exception as exc:
            logger.warning("health: не удалось получить getMe ({})", str(exc)[:160])
    return runner


# --------------------------------------------------------------------------- #
# Preflight
# --------------------------------------------------------------------------- #
def odds_source_problem() -> str | None:
    """Проблема с источниками кэфов или None, если источник хотя бы один настроен.

    Источник считается настроенным, если задан WINLINE_API_BASE и/или
    BETBOOM_API_BASE либо THE_ODDS_API_KEY при включённом THE_ODDS_API_ENABLED
    (см. Settings.odds_providers_configured).
    """
    if settings.odds_providers_configured:
        return None
    return (
        "Ни один источник кэфов не настроен — кэфы брать негде. "
        "Задайте WINLINE_API_BASE и/или BETBOOM_API_BASE "
        "(см. SETUP.md → «Как достать URL букмекера через DevTools») "
        "либо THE_ODDS_API_KEY + THE_ODDS_API_ENABLED=true (TheOddsApi)"
    )


async def preflight() -> bool:
    """Проверки до старта: БД, таблицы, ключи. Возвращает True, если можно работать."""
    problems: list[str] = []

    if not await healthcheck():
        problems.append("БД недоступна (проверьте DATABASE_URL — на Railway его подставляет плагин PostgreSQL)")

    if not settings.openrouter_api_key:
        problems.append("OPENROUTER_API_KEY пуст — LLM-уровни работать не будут")
    if not settings.telegram_bot_token:
        problems.append("TELEGRAM_BOT_TOKEN пуст — рассылка невозможна")
    odds_problem = odds_source_problem()
    if odds_problem:
        problems.append(odds_problem)
    if settings.judge_enabled and not settings.judge_model:
        problems.append("JUDGE_ENABLED=true, но JUDGE_MODEL пуст")

    try:
        from app.db.models import ALL_TABLES
        from app.db.database import get_engine

        engine = get_engine()
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1 FROM sports LIMIT 1"))
    except Exception as exc:
        problems.append(
            f"Таблиц нет или схема устарела ({str(exc)[:120]}). Выполните: alembic upgrade head "
            "(или CREATE_ALL=true — но это только для локальных тестов)"
        )

    if problems:
        for problem in problems:
            logger.error("preflight: {}", problem)
        logger.warning(
            "preflight: проблем — {}. Сервис поднимется в деградированном режиме — "
            "healthcheck покажет статус, сигналы не будут рассылаться до устранения.",
            len(problems),
        )
        return False
    logger.info("preflight: все проверки пройдены")
    return True


# --------------------------------------------------------------------------- #
# Graceful shutdown
# --------------------------------------------------------------------------- #
async def shutdown(bot: Any, dispatcher: Any, scheduler: Any) -> None:
    logger.info("shutdown: останавливаю сервис…")
    try:
        if bot is not None:
            await bot.session.close()
    except Exception as exc:
        logger.debug("shutdown: bot.session.close ({})", exc)
    try:
        if scheduler is not None and scheduler.running:
            scheduler.shutdown(wait=False)
    except Exception as exc:
        logger.debug("shutdown: scheduler.shutdown ({})", exc)
    try:
        await shutdown_providers()
    except Exception as exc:
        logger.debug("shutdown: shutdown_providers ({})", exc)
    try:
        from app.llm_client import close_llm_client

        await close_llm_client()
    except Exception as exc:
        logger.debug("shutdown: close_llm_client ({})", exc)
    try:
        await dispose_engine()
    except Exception as exc:
        logger.debug("shutdown: dispose_engine ({})", exc)
    logger.info("shutdown: завершено")


# --------------------------------------------------------------------------- #
# Точка входа
# --------------------------------------------------------------------------- #
async def run() -> None:
    setup_logging()
    logger.info(
        "BetSignals: запуск (TZ={}, окружение={})",
        settings.tz,
        os.environ.get("RAILWAY_ENVIRONMENT_NAME", "local"),
    )

    init_engine()
    if settings.create_all:
        # Только для локальных тестов: в продакшене схему создают миграции Alembic.
        from app.db.database import create_all

        await create_all()
        logger.warning("BetSignals: CREATE_ALL=true — схема создана через SQLAlchemy (не для продакшена)")
    await ensure_sports()
    ready = await preflight()

    bot = None
    dispatcher = None
    scheduler = None
    health_runner = None

    try:
        if settings.telegram_bot_token:
            bot = create_bot()
            dispatcher = create_dispatcher()
        else:
            logger.error("BetSignals: TELEGRAM_BOT_TOKEN пуст — бот не запускается, только анализ и БД")

        health_runner = await start_health_server(bot)

        if bot is not None:
            await notify_admins(
                bot,
                "🚀 <b>BetSignals запущен</b>\n"
                f"Модели: {settings.screener_model} → {settings.analyzer_model} → {settings.judge_model}\n"
                f"Арбитр: {'включён' if settings.judge_enabled else 'ВЫКЛЮЧЕН (JUDGE_ENABLED=false)'}\n"
                f"Расписание: сбор {settings.collect_hours}:00 МСК, лайв каждые {settings.live_poll_seconds}с",
            )

        scheduler = create_scheduler(bot)
        scheduler.start()
        for job in list_jobs(scheduler):
            logger.info("scheduler: {} | {} | next={}", job["id"], job["trigger"], job["next_run"])

        # Небольшая «разогревающая» инициализация: спорт-строки и провайдеры.
        logger.info("BetSignals: источники — {}", get_providers().describe())

        if not ready:
            logger.warning(
                "BetSignals: preflight нашёл проблемы — сервис работает в деградированном режиме. "
                "Проверьте /health и SETUP.md."
            )

        if bot is not None and dispatcher is not None:
            logger.info("BetSignals: стартую Telegram polling")
            await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types())
        else:
            logger.info("BetSignals: работаю без бота — жду сигналов SIGTERM/SIGINT")
            while True:
                await asyncio.sleep(3600)
    except asyncio.CancelledError:
        logger.info("BetSignals: получена отмена задачи")
    finally:
        if health_runner is not None:
            try:
                await health_runner.cleanup()
            except Exception as exc:
                logger.debug("shutdown: health runner cleanup ({})", exc)
        await shutdown(bot, dispatcher, scheduler)


def main() -> None:
    """Синхронный вход + корректная обработка SIGTERM (Railway останавливает сервис им)."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    main_task = loop.create_task(run())

    def _signal_handler() -> None:
        logger.info("BetSignals: получен сигнал остановки")
        main_task.cancel()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:  # Windows
            signal.signal(sig, lambda *_: _signal_handler())

    try:
        loop.run_until_complete(main_task)
    except asyncio.CancelledError:
        logger.info("BetSignals: остановлен по сигналу")
    finally:
        try:
            loop.run_until_complete(asyncio.sleep(0.1))
        except Exception:
            pass
        loop.close()
        logger.info("BetSignals: процесс завершён")


if __name__ == "__main__":
    main()
