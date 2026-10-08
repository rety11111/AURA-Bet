"""Регрессионные тесты регистрации задач APScheduler.

История бага: задачи добавлялись sync-лямбдами `lambda: _safe_job(...)`, которые
возвращают coroutine. AsyncIOExecutor (apscheduler.executors.asyncio) запускает
coroutine-функции в event loop и ждёт их, а всё остальное — в thread pool через
run_job(), который возвращает coroutine НЕ дожидаясь. В логах это выглядело как
`RuntimeWarning: coroutine '_safe_job' was never awaited`, а задачи молча
не выполнялись. Тесты ниже ловят обе стороны ошибки: контракт регистрации и
реальный прогон через настоящий AsyncIOExecutor.
"""

from __future__ import annotations

import asyncio
import gc
import inspect
import warnings
from datetime import datetime, timezone

from apscheduler.executors.asyncio import AsyncIOExecutor
from apscheduler.util import iscoroutinefunction_partial

from app.scheduler import _safe_job, create_scheduler

EXPECTED_JOB_IDS = {
    "collect_schedule",
    "refresh_odds",
    "prematch_passes",
    "live_worker",
    "results",
    "daily_learning",
    "self_review",
    "daily_digest",
    "broadcast",
}

# Имена точек входа в app.scheduler, которые вызывает каждая задача (job id → атрибут модуля).
JOB_ENTRYPOINTS = {
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


def test_create_scheduler_registers_all_nine_jobs():
    scheduler = create_scheduler(bot=None)
    jobs = scheduler.get_jobs()
    assert len(jobs) == 9
    assert {job.id for job in jobs} == EXPECTED_JOB_IDS
    # У каждой задачи есть триггер и понятное имя для логов.
    for job in jobs:
        assert job.trigger is not None
        assert job.name


def test_jobs_registered_as_coroutine_functions():
    """Регрессия: func каждой задачи — coroutine-функция (_safe_job), а не sync-лямбда.

    Именно по этому признаку AsyncIOExecutor выбирает event loop вместо thread pool
    (см. apscheduler.executors.asyncio.AsyncIOExecutor._do_submit_job).
    """
    scheduler = create_scheduler(bot=None)
    for job in scheduler.get_jobs():
        assert job.func is _safe_job, (
            f"задача {job.id} зарегистрирована не через _safe_job: {job.func!r}"
        )
        assert iscoroutinefunction_partial(job.func), (
            f"задача {job.id} зарегистрирована sync-функцией {job.func!r} — "
            "AsyncIOExecutor выполнит её в thread pool и не дождётся coroutine "
            "(RuntimeWarning: coroutine '_safe_job' was never awaited)"
        )
        # args=[имя, фабрика coroutine]: _safe_job(name, coro_factory)
        assert len(job.args) == 2
        assert isinstance(job.args[0], str) and job.args[0]
        assert callable(job.args[1])


async def test_jobs_execute_via_real_asyncio_executor(monkeypatch):
    """Прогон через настоящий AsyncIOExecutor: каждая задача реально выполняется.

    Заглушки вместо настоящих job-функций; диспетчеризация — как у запущенного
    AsyncIOScheduler. Со старой регистрацией (sync-лямбда) задачи молча не
    выполнялись бы, а тест ловил бы и RuntimeWarning про never awaited.
    """
    import app.scheduler as scheduler_module

    executed: list[str] = []

    def make_stub(job_id: str):
        async def stub(*_args, **_kwargs):
            executed.append(job_id)
            return {}

        return stub

    # Патчим точки входа ДО create_scheduler: фабрики в args берут их из модуля.
    for job_id, attr in JOB_ENTRYPOINTS.items():
        monkeypatch.setattr(scheduler_module, attr, make_stub(job_id))

    scheduler = create_scheduler(bot=None)
    executor = AsyncIOExecutor()
    # Обвязка как у работающего AsyncIOScheduler: executor.start берёт event loop оттуда.
    scheduler._eventloop = asyncio.get_running_loop()
    executor.start(scheduler, "default")

    run_time = datetime.now(timezone.utc)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        for job in scheduler.get_jobs():
            executor._do_submit_job(job, [run_time])
        # Ждём все future, которые создал executor (и coroutine-ветку, и thread pool).
        pending = list(executor._pending_futures)
        if pending:
            await asyncio.gather(*pending)
        await asyncio.sleep(0)  # дать done-callbacks отработать
        gc.collect()  # never-awaited coroutine предупреждение появляется при сборке мусора

    never_awaited = [
        w for w in caught
        if issubclass(w.category, RuntimeWarning) and "never awaited" in str(w.message)
    ]
    assert not never_awaited, f"корутины не дождались: {[str(w.message) for w in never_awaited]}"
    assert sorted(executed) == sorted(EXPECTED_JOB_IDS), (
        f"выполнились не все задачи: {sorted(executed)}"
    )


async def test_safe_job_awaits_factory_and_swallows_errors():
    """_safe_job действительно ждёт фабрику и не даёт ошибке уронить процесс."""
    calls: list[str] = []

    async def ok_factory():
        calls.append("ok")
        return {"done": 1}

    async def bad_factory():
        calls.append("bad")
        raise RuntimeError("boom")

    await _safe_job("ok_job", ok_factory)
    await _safe_job("bad_job", bad_factory)  # не должно пробросить исключение
    assert calls == ["ok", "bad"]
