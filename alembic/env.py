"""Alembic environment для async SQLAlchemy (Модуль 12 ТЗ).

URL базы берётся из настроек приложения (settings.database_url → уже приведён
к драйверу asyncpg валидатором в app/config.py). Это значит, что миграции
применяются к ТОЙ ЖЕ базе, что и сервис: и локально, и на Railway.

Команды:
  alembic upgrade head      — применить миграции (делайте это в release-скрипте Railway)
  alembic downgrade -1      — откатить последнюю
  alembic revision --autogenerate -m "..."  — новая миграция по изменениям моделей
"""

from __future__ import annotations

import asyncio
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.config import settings
from app.db.models import Base

config = context.config

if config.config_file_name is not None:
    # fileConfig ломается, если в alembic.ini нет секции [loggers] — у нас есть.
    fileConfig(config.config_file_name)

# URL из настроек приложения (перекрывает alembic.ini)
config.set_main_option("sqlalchemy.url", settings.database_url)

target_metadata = Base.metadata


def include_object(object_, name, type_, reflected, compare_to) -> bool:  # type: ignore[no-untyped-def]
    """Служебные таблицы не трогаем (alembic_version создаётся сам)."""
    if type_ == "table" and name in {"alembic_version", "spatial_ref_sys"}:
        return False
    return True


def run_migrations_offline() -> None:
    """Офлайн-режим: генерируем SQL без подключения к БД."""
    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
