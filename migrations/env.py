"""Окружение Alembic: та же база и те же модели, что у бота.

Адрес базы - sqlalchemy.url из конфига Alembic (так его задают тесты), иначе
DATABASE_URL из настроек бота. Драйвер асинхронный (asyncpg, aiosqlite), как у бота.
"""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from config import settings
from database import Base

config = context.config

# логи из alembic.ini - только при запуске из командной строки: в тестах fileConfig
# перенастроил бы логирование pytest
if config.config_file_name is not None and config.attributes.get(
    "configure_logger", True
):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _url() -> str:
    return config.get_main_option("sqlalchemy.url") or settings.database_url


def _configure(**kwargs) -> None:
    context.configure(
        target_metadata=target_metadata,
        compare_type=True,
        # SQLite не умеет ALTER COLUMN: Alembic пересоздаёт таблицу (стенд на SQLite)
        render_as_batch=_url().startswith("sqlite"),
        **kwargs,
    )


def run_migrations_offline() -> None:
    """`alembic upgrade head --sql`: напечатать SQL, не соединяясь с базой."""
    _configure(url=_url(), literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def _run_sync(connection: Connection) -> None:
    _configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = create_async_engine(_url())
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_run_sync)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
