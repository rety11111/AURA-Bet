"""Общие фикстуры тестов.

ВАЖНО: DATABASE_URL подменяется на sqlite ДО импорта app-модулей, чтобы тесты
не требовали PostgreSQL. Модели используют JSONB-variant, поэтому sqlite работает.
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("OPENROUTER_API_KEY", "")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:TEST-TOKEN")

import pytest  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402

from app.db.models import Base  # noqa: E402


@pytest.fixture
async def engine():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
async def session(engine) -> AsyncSession:
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as session:
        yield session


@pytest.fixture
async def sport_id(session) -> int:
    """Создаёт вид спорта «football» и возвращает его id."""
    from app.db.models import Sport

    sport = Sport(code="football", name="Футбол")
    session.add(sport)
    await session.flush()
    return sport.id
