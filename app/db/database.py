"""Асинхронный слой доступа к БД: engine, session factory, хелперы.

Одна долгоживущая asyncio-задача (Railway web-процесс) держит один engine.
Для тестов поддерживается sqlite+aiosqlite (см. tests/conftest.py).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from loguru import logger
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.db.models import Base, SPORT_NAMES, Sport

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def build_engine(url: str | None = None) -> AsyncEngine:
    """Создаёт engine с параметрами, безопасными для postgres и sqlite."""
    url = url or settings.database_url
    kwargs: dict = {"echo": settings.db_echo, "pool_pre_ping": True, "future": True}
    if url.startswith("sqlite"):
        kwargs = {"echo": settings.db_echo, "future": True}
    else:
        kwargs.update(
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            connect_args={"timeout": settings.db_connect_timeout_sec},
        )
    return create_async_engine(url, **kwargs)


def init_engine(url: str | None = None) -> AsyncEngine:
    global _engine, _session_factory
    if _engine is None:
        _engine = build_engine(url)
        _session_factory = async_sessionmaker(_engine, expire_on_commit=False, class_=AsyncSession)
        logger.info("DB engine создан: {}", (url or settings.database_url).split("@")[-1])
    return _engine


def get_engine() -> AsyncEngine:
    if _engine is None:
        return init_engine()
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    if _session_factory is None:
        init_engine()
    assert _session_factory is not None
    return _session_factory


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Транзакционная сессия: commit при успехе, rollback при исключении."""
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def create_all() -> None:
    """Создать схему без alembic (используется в тестах и как аварийный bootstrap)."""
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Схема БД создана (create_all)")


async def ensure_sports() -> None:
    """Наполнить справочник видов спорта (идемпотентно)."""
    async with session_scope() as session:
        existing = (await session.execute(select(Sport.code))).scalars().all()
        known = set(existing)
        for code, name in SPORT_NAMES.items():
            if code not in known:
                session.add(Sport(code=code, name=name))
        await session.flush()


async def healthcheck() -> bool:
    try:
        engine = get_engine()
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception as exc:  # noqa: BLE001 — healthcheck не должен падать
        logger.error("DB healthcheck FAILED: {}", exc)
        return False


async def dispose_engine() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None
        logger.info("DB engine закрыт")


async def get_sport_id(session: AsyncSession, code: str) -> int | None:
    return (await session.execute(select(Sport.id).where(Sport.code == code))).scalar_one_or_none()


async def get_sport_map(session: AsyncSession) -> dict[str, int]:
    rows = (await session.execute(select(Sport.code, Sport.id))).all()
    return {code: sport_id for code, sport_id in rows}
