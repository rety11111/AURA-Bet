"""Планировщик APScheduler (Модуль 11 ТЗ) — ЕДИНЫЙ процесс, без cron-сервисов.

Расписание (все времена — Europe/Moscow, settings.tz):
  • 06:00 и 14:00 — сбор расписания на день (collect_hours="6,14");
  • каждые 30 минут — обновление кэфов (odds_refresh_minutes);
  • каждые 10 минут — проходы pre-match анализа: PASS 1 (T−6ч) и PASS 2 (T−90м)
    + закрытие устаревших кандидатов, у которых матч начался;
  • каждые 45 секунд — лайв-воркер киберспорта (Dota 2 / CS2);
  • каждый час — трекер результатов (settle_finished_matches);
  • 04:00 — обучение (калибровка лиг + веса ансамбля);
  • воскресенье 05:00 — недельный self-review лиг (инсайты в промпты);
  • 23:59 — дневная сводка подписчикам;
  • каждую минуту — рассылка подтверждённых сигналов (broadcast_pending).

ВАЖНО: Railway НЕ должен запускать эти задачи отдельными cron-сервисами —
всё живёт в одном процессе `python -m app.main` (см. Procfile и ТЗ).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from functools import partial
from typing import Any

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from loguru import logger

from app.bot.broadcaster import broadcast_pending, send_daily_digest
from app.config import settings
from app.db.database import session_scope
from app.learning.calibration import run_daily_learning
from app.learning.self_review import run_self_review
from app.live.esports_worker import cancel_stale_live_signals, live_tick
from app.pipeline.analyzer import reject_stale_candidates, run_late_matches, run_prematch_pass
from app.pipeline.collector import (
    collect_day_schedule,
    get_providers,
    refresh_todays_odds,
)
from app.tracking.results_tracker import settle_finished_matches


async def _safe_job(name: str, coro_factory: Callable[[], Awaitable[Any]]) -> None:
    """Обёртка: ни одна ошибка задачи не должна уронить процесс (ТЗ: graceful degradation)."""
    try:
        result = await coro_factory()
        if result is not None:
            logger.debug("scheduler: {} завершена → {}", name, result)
        else:
            logger.debug("scheduler: {} завершена", name)
    except Exception as exc:
        logger.exception("scheduler: задача {} упала — {}", name, str(exc)[:300])


# --------------------------------------------------------------------------- #
# Задачи
# --------------------------------------------------------------------------- #
async def job_collect_schedule() -> dict[str, int]:
    """Сбор расписания на день."""
    providers = get_providers()
    async with session_scope() as session:
        return await collect_day_schedule(session, providers=providers)


async def job_refresh_odds() -> int:
    """Обновление кэфов по матчам ближайших суток."""
    providers = get_providers()
    async with session_scope() as session:
        return await refresh_todays_odds(session, providers)


async def job_prematch_passes() -> dict[str, int]:
    """PASS 1 (T−6ч) и PASS 2 (T−90м) + добор поздних матчей."""
    providers = get_providers()
    counters: dict[str, int] = {}
    async with session_scope() as session:
        counters["pass1"] = await run_prematch_pass(session, pass_no=1, providers=providers)
    async with session_scope() as session:
        counters["pass2"] = await run_prematch_pass(session, pass_no=2, providers=providers)
    async with session_scope() as session:
        counters["late"] = await run_late_matches(session, providers)
    async with session_scope() as session:
        counters["stale"] = await reject_stale_candidates(session)
    return counters


async def job_live_worker() -> dict[str, int]:
    """Лайв-воркер киберспорта: опрос 45 секунд, окна драфта/раундов."""
    providers = get_providers()
    counters: dict[str, int] = {}
    async with session_scope() as session:
        counters.update(await live_tick(session, providers))
    async with session_scope() as session:
        counters["cancelled"] = await cancel_stale_live_signals(session)
    return counters


async def job_results() -> dict[str, int]:
    """Трекер результатов (ежечасно) — кормит обучение и статистику."""
    providers = get_providers()
    async with session_scope() as session:
        return await settle_finished_matches(session, providers)


async def job_daily_learning() -> dict[str, Any]:
    async with session_scope() as session:
        return await run_daily_learning(session)


async def job_self_review() -> list[dict[str, Any]]:
    async with session_scope() as session:
        return await run_self_review(session)


async def job_broadcast(bot: Bot | None) -> int:
    if bot is None:
        logger.debug("scheduler: бот не запущен — рассылка пропущена")
        return 0
    return await broadcast_pending(bot)


async def job_digest(bot: Bot | None) -> int:
    if bot is None or not settings.daily_digest_enabled:
        return 0
    return await send_daily_digest(bot)


# --------------------------------------------------------------------------- #
# Фабрика
# --------------------------------------------------------------------------- #
def create_scheduler(bot: Bot | None = None) -> AsyncIOScheduler:
    """Создаёт (но не запускает) планировщик со всеми задачами.

    ВАЖНО: передаём `_safe_job` (async-функцию) напрямую, а тело задачи — через args.
    Нельзя использовать `lambda: _safe_job(...)`: AsyncIOScheduler не await-ит результат
    не-coroutine функций, и задача молча не выполняется (RuntimeWarning: never awaited).
    См. tests/test_scheduler.py.
    """
    scheduler = AsyncIOScheduler(timezone=settings.tz)

    collect_hours = [hour.strip() for hour in settings.collect_hours.split(",") if hour.strip()]
    scheduler.add_job(
        _safe_job,
        CronTrigger(hour=",".join(collect_hours), minute=0, timezone=settings.tz),
        args=("collect_schedule", job_collect_schedule),
        id="collect_schedule",
        name="Сбор расписания (06:00/14:00 МСК)",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=600,
    )
    scheduler.add_job(
        _safe_job,
        IntervalTrigger(minutes=settings.odds_refresh_minutes, timezone=settings.tz),
        args=("refresh_odds", job_refresh_odds),
        id="refresh_odds",
        name=f"Обновление кэфов (каждые {settings.odds_refresh_minutes} мин)",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
    )
    scheduler.add_job(
        _safe_job,
        IntervalTrigger(minutes=10, timezone=settings.tz),
        args=("prematch_passes", job_prematch_passes),
        id="prematch_passes",
        name="PASS 1 (T−6ч) / PASS 2 (T−90м) каждые 10 минут",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=240,
    )
    scheduler.add_job(
        _safe_job,
        IntervalTrigger(seconds=settings.live_poll_seconds, timezone=settings.tz),
        args=("live_worker", job_live_worker),
        id="live_worker",
        name=f"Лайв-воркер киберспорта ({settings.live_poll_seconds} с)",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=30,
    )
    scheduler.add_job(
        _safe_job,
        CronTrigger(minute=7, timezone=settings.tz),  # ежечасно в :07, чтобы не пересекаться со сбором
        args=("results", job_results),
        id="results",
        name="Трекер результатов (ежечасно)",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=900,
    )
    scheduler.add_job(
        _safe_job,
        CronTrigger(hour=settings.calibration_hour, minute=0, timezone=settings.tz),
        args=("daily_learning", job_daily_learning),
        id="daily_learning",
        name=f"Обучение: калибровка + веса ({settings.calibration_hour}:00 МСК)",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=1800,
    )
    scheduler.add_job(
        _safe_job,
        CronTrigger(
            day_of_week=settings.self_review_weekday,
            hour=settings.self_review_hour,
            minute=0,
            timezone=settings.tz,
        ),
        args=("self_review", job_self_review),
        id="self_review",
        name=f"Self-review лиг (вс {settings.self_review_hour}:00 МСК)",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )
    scheduler.add_job(
        _safe_job,
        CronTrigger(hour=settings.digest_hour, minute=settings.digest_minute, timezone=settings.tz),
        args=("daily_digest", partial(job_digest, bot)),
        id="daily_digest",
        name=f"Дневная сводка ({settings.digest_hour}:{settings.digest_minute:02d} МСК)",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=900,
    )
    scheduler.add_job(
        _safe_job,
        IntervalTrigger(minutes=1, timezone=settings.tz),
        args=("broadcast", partial(job_broadcast, bot)),
        id="broadcast",
        name="Рассылка подтверждённых сигналов",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=120,
    )
    logger.info("scheduler: создан, задач — {}", len(scheduler.get_jobs()))
    return scheduler


def list_jobs(scheduler: AsyncIOScheduler) -> list[dict[str, Any]]:
    """Список задач с расписанием — печатается в лог при старте и в scripts/check_sources.py."""
    jobs: list[dict[str, Any]] = []
    for job in scheduler.get_jobs():
        jobs.append(
            {
                "id": job.id,
                "name": job.name,
                "trigger": str(job.trigger),
                "next_run": job.next_run_time.isoformat() if getattr(job, "next_run_time", None) else None,
            }
        )
    return jobs
