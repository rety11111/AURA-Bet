"""Регрессионные тесты регистрации задач APScheduler.

Баг (до фикса): задачи добавлялись как sync-лямбды вида
    lambda: _safe_job("name", job_fn)
AsyncIOScheduler выполняет НЕ-coroutine функции в thread pool и не await-ит результат,
поэтому лямбда лишь создавала coroutine `_safe_job(...)`, который никто не ждал:
задача «успешно» завершалась, но её тело не выполнялось, а в логах появлялось
`RuntimeWarning: coroutine '_safe_job' was never awaited`.

Тесты ловят это двумя способами:
  1) структурно — у каждой из 9 задач func это async-функция `_safe_job`;
  2) поведенчески — реальный AsyncIOScheduler запускает каждую задачу, и её тело
     (подменённое на recorder) обязано отработать.
"""

from __future__ import annotations

import asyncio
import inspect
from datetime import UTC, datetime

import pytest

import app.scheduler as scheduler_module
from app.scheduler import _safe_job, create_scheduler

# job_id → имя функции-тела задачи в модуле app.scheduler
JOB_BODIES: dict[str, str] = {
    "collect_schedule": "job_collect_schedule",
    "refresh_odds": "job_refresh_odds",
    "prematch_passes": "job_prematch_passes",
    "live_worker": "job_live_worker",
    "results": "job_results",
    "daily_learning": "job_daily_learning",
    "self_review": "job_self_review",
    "daily_digest": "job_digest",
    "broadcast": "job_broadcast",
}


def test_all_nine_jobs_are_registered() -> None:
    scheduler = create_scheduler(None)
    assert {job.id for job in scheduler.get_jobs()} == set(JOB_BODIES)


def test_every_job_is_a_native_coroutine_wrapper() -> None:
    """Главная проверка против регрессии: APScheduler должен видеть async-функцию напрямую."""
    scheduler = create_scheduler(None)
    for job in scheduler.get_jobs():
        assert job.func is _safe_job, f"{job.id}: func должен быть _safe_job, а не {job.func!r}"
        assert inspect.iscoroutinefunction(job.func), f"{job.id}: _safe_job должна быть async-функцией"
        # Первый аргумент — имя задачи для логов; второй — фабрика coroutine тела задачи.
        assert job.args[0] == job.id
        assert callable(job.args[1])


async def test_safe_job_awaits_body_and_swallows_errors() -> None:
    """Обёртка сохраняет поведение: успешная задача awaited, падение задачи не роняет процесс."""
    called: list[str] = []

    async def ok() -> int:
        called.append("ok")
        return 1

    async def boom() -> None:
        raise RuntimeError("boom")

    await _safe_job("ok", ok)
    await _safe_job("boom", boom)  # не должно пробросить исключение
    assert called == ["ok"]


async def test_every_job_body_actually_runs_under_asyncio_scheduler(monkeypatch: pytest.MonkeyPatch) -> None:
    """Поведенческий тест: реальный AsyncIOScheduler обязан выполнить все 9 задач.

    Тела задач подменяем recorder-ами, которые ставят Event. create_scheduler читает
    имена job_* из модуля в момент создания, поэтому подмена делается до него.
    """
    events = {job_id: asyncio.Event() for job_id in JOB_BODIES}

    def make_recorder(job_id: str):
        async def recorder(*_args: object, **_kwargs: object) -> int:
            events[job_id].set()
            return 0

        return recorder

    for job_id, attr in JOB_BODIES.items():
        monkeypatch.setattr(scheduler_module, attr, make_recorder(job_id))

    scheduler = create_scheduler(None)
    scheduler.start()
    try:
        now = datetime.now(UTC)
        for job in scheduler.get_jobs():
            job.modify(next_run_time=now)  # запускаем все задачи немедленно
        await asyncio.wait_for(
            asyncio.gather(*(event.wait() for event in events.values())),
            timeout=10,
        )
    finally:
        scheduler.shutdown(wait=False)

    not_run = sorted(job_id for job_id, event in events.items() if not event.is_set())
    assert not not_run, f"задачи не выполнились (coroutine не был await-ован): {not_run}"
