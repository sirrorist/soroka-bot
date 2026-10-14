"""Миграции Alembic: ревизии дают ту же схему, что модели; бот на чужой ревизии не стартует.

SQLite - всегда. PostgreSQL (как на проде) - если задан TEST_POSTGRES_URL с правом
создавать базы: в CI его даёт job `migrations`, локально - контейнер, commands.md.
"""

import asyncio
import io
import os
import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from alembic.util.exc import CommandError
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

import database
import main

ROOT = Path(__file__).resolve().parent.parent
PG_URL = os.environ.get("TEST_POSTGRES_URL")


def alembic_config(url: str, output_buffer=None) -> Config:
    """Настоящий alembic.ini из корня, но база - тестовая, а логи - не трогать."""
    config = Config(str(ROOT / "alembic.ini"), output_buffer=output_buffer)
    config.set_main_option("sqlalchemy.url", url)
    config.attributes["configure_logger"] = False
    return config


async def alembic(fn, *args, **kwargs):
    """Команда Alembic из асинхронного теста: env.py зовёт asyncio.run, а цикл теста
    уже работает - поэтому в отдельном потоке, как из командной строки."""
    await asyncio.to_thread(fn, *args, **kwargs)


async def schema_diff(url: str) -> list:
    """Чем схема базы отличается от моделей database.py (пусто - ничем)."""
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            return await conn.run_sync(
                lambda sync_conn: compare_metadata(
                    MigrationContext.configure(
                        sync_conn,
                        # server_default: модели его не задают (значения - на стороне
                        # Python), и в базе его быть не должно
                        opts={"compare_type": True, "compare_server_default": True},
                    ),
                    database.Base.metadata,
                )
            )
    finally:
        await engine.dispose()


async def current_revision(url: str) -> str | None:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            return await conn.run_sync(
                lambda c: MigrationContext.configure(c).get_current_revision()
            )
    finally:
        await engine.dispose()


@pytest.fixture
def sqlite_url(tmp_path):
    return f"sqlite+aiosqlite:///{tmp_path / 'migrations.db'}"


# ---------- ревизии ----------


def test_one_head_and_baseline_is_0001():
    script = ScriptDirectory.from_config(alembic_config("sqlite://"))
    assert len(script.get_heads()) == 1
    assert script.get_base() == "0001"
    # константа в database.py, по которой бот сверяет базу, - последняя ревизия
    assert database.SCHEMA_REVISION == script.get_current_head()


async def test_upgrade_head_on_empty_sqlite_matches_models(sqlite_url):
    await alembic(command.upgrade, alembic_config(sqlite_url), "head")

    assert await schema_diff(sqlite_url) == []
    assert await current_revision(sqlite_url) == database.SCHEMA_REVISION


async def test_upgrade_head_twice_changes_nothing(sqlite_url):
    config = alembic_config(sqlite_url)
    await alembic(command.upgrade, config, "head")
    await alembic(command.upgrade, config, "head")

    assert await schema_diff(sqlite_url) == []


async def test_stamp_on_create_all_database_marks_without_touching_tables(
    sqlite_url,
):
    """Как на проде: база создана create_all, stamp ставит отметку, таблицы не трогает."""
    engine = create_async_engine(sqlite_url)
    async with engine.begin() as conn:
        await conn.run_sync(database.Base.metadata.create_all)
        await conn.execute(text("INSERT INTO users (id, first_name) VALUES (7, 'Аня')"))
    await engine.dispose()

    await alembic(command.stamp, alembic_config(sqlite_url), "0001")

    assert await current_revision(sqlite_url) == "0001"
    engine = create_async_engine(sqlite_url)
    async with engine.connect() as conn:
        names = (await conn.execute(text("SELECT first_name FROM users"))).all()
    await engine.dispose()
    assert names == [("Аня",)]


async def test_alembic_check_sees_type_drift_through_env_py(sqlite_url):
    """`alembic check` (и будущий autogenerate) берёт настройки сравнения из env.py:
    без compare_type смена длины строки в моделях прошла бы незамеченной."""
    config = alembic_config(sqlite_url)
    await alembic(command.upgrade, config, "head")
    await alembic(command.check, config)  # без расхождений - тишина

    engine = create_async_engine(sqlite_url)
    async with engine.begin() as conn:
        await conn.execute(text("DROP TABLE user_settings"))
        await conn.execute(
            text(
                "CREATE TABLE user_settings (user_id BIGINT NOT NULL, lang VARCHAR(2),"
                " updated_at DATETIME, PRIMARY KEY (user_id))"
            )
        )
    await engine.dispose()

    with pytest.raises(CommandError, match="New upgrade operations detected"):
        await alembic(command.check, config)


def test_baseline_downgrade_is_refused(sqlite_url):
    config = alembic_config(sqlite_url)
    command.upgrade(config, "head")
    with pytest.raises(RuntimeError, match="запрещён"):
        command.downgrade(config, "base")


def test_offline_sql_creates_all_tables():
    """`alembic upgrade head --sql` - SQL без соединения с базой, для проверки глазами."""
    buffer = io.StringIO()
    command.upgrade(
        alembic_config("postgresql+asyncpg://offline/x", buffer), "head", sql=True
    )
    sql = buffer.getvalue()
    for table in ("users", "downloads", "user_settings", "chats", "alembic_version"):
        assert f"CREATE TABLE {table}" in sql


# ---------- check_schema ----------


async def test_check_schema_passes_at_head(sqlite_url):
    await alembic(command.upgrade, alembic_config(sqlite_url), "head")
    engine = create_async_engine(sqlite_url)
    try:
        await database.check_schema(engine)
    finally:
        await engine.dispose()


async def test_check_schema_without_mark_names_both_ways_out(sqlite_url):
    engine = create_async_engine(sqlite_url)
    async with engine.begin() as conn:
        await conn.run_sync(database.Base.metadata.create_all)
    try:
        with pytest.raises(database.SchemaNotReady) as err:
            await database.check_schema(engine)
    finally:
        await engine.dispose()
    assert "нет отметки" in str(err.value)
    # подсказки дословно: оба пути (пустая база и база до миграций) и ревизия кода
    assert "Пустая база - `alembic upgrade head`" in str(err.value)
    assert "`alembic stamp 0001`, затем `alembic upgrade head`" in str(err.value)
    assert "код ждёт ревизию 0001" in str(err.value)


async def test_check_schema_on_other_revision_asks_for_upgrade(sqlite_url):
    await alembic(command.upgrade, alembic_config(sqlite_url), "head")
    engine = create_async_engine(sqlite_url)
    async with engine.begin() as conn:
        await conn.execute(text("UPDATE alembic_version SET version_num = 'zzzz'"))
    try:
        with pytest.raises(database.SchemaNotReady) as err:
            await database.check_schema(engine)
    finally:
        await engine.dispose()
    assert "zzzz" in str(err.value)
    assert database.SCHEMA_REVISION in str(err.value)
    assert "База на ревизии zzzz, код ждёт 0001: `alembic upgrade head`" in str(
        err.value
    )
    # ревизия базы новее кода - не "обновить", а "запущен старый образ"
    assert "запущен старый образ" in str(err.value)


async def test_check_schema_on_empty_alembic_version_says_no_mark(sqlite_url):
    """Таблица alembic_version есть, строки нет (stamp/upgrade оборвались) - как без неё."""
    await alembic(command.upgrade, alembic_config(sqlite_url), "head")
    engine = create_async_engine(sqlite_url)
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM alembic_version"))
    try:
        with pytest.raises(database.SchemaNotReady) as err:
            await database.check_schema(engine)
    finally:
        await engine.dispose()
    assert "нет отметки" in str(err.value)


async def test_check_schema_on_empty_database_changes_nothing(sqlite_url):
    """Бот схему не создаёт: на пустой базе отказ, и ни таблиц, ни отметки после него."""
    engine = create_async_engine(sqlite_url)
    try:
        with pytest.raises(database.SchemaNotReady, match="нет отметки"):
            await database.check_schema(engine)
        async with engine.connect() as conn:
            tables = (await conn.execute(text("SELECT name FROM sqlite_master"))).all()
    finally:
        await engine.dispose()
    assert tables == []


def stub_main_until_bot(monkeypatch, check):
    """main() с подменённой check_schema; создание Bot - ошибка теста."""

    def no_bot(*args, **kwargs):
        raise AssertionError("до проверки схемы бот не создаётся")

    monkeypatch.setattr(main, "check_schema", check)
    monkeypatch.setattr(main, "setup_logging", lambda: None)
    monkeypatch.setattr(main, "Bot", no_bot)


async def test_main_does_not_touch_telegram_when_schema_not_ready(monkeypatch):
    async def not_ready():
        raise database.SchemaNotReady("нет отметки")

    stub_main_until_bot(monkeypatch, not_ready)

    with pytest.raises(SystemExit) as err:
        await main.main()
    assert err.value.code == 1


async def test_main_logs_the_reason_before_exit(monkeypatch):
    """Оператор видит в логе, почему бот не стартовал и что делать."""

    async def not_ready():
        raise database.SchemaNotReady("нет отметки; alembic upgrade head")

    stub_main_until_bot(monkeypatch, not_ready)
    records = []
    sink = main.logger.add(lambda m: records.append(m.record), level="ERROR")
    try:
        with pytest.raises(SystemExit):
            await main.main()
    finally:
        main.logger.remove(sink)

    assert len(records) == 1
    assert records[0]["level"].name == "ERROR"
    assert "нет отметки; alembic upgrade head" in records[0]["message"]


async def test_main_does_not_hide_other_database_errors(monkeypatch):
    """Недоступная база - не "схема не готова": ошибка уходит наверх как есть."""

    async def db_down():
        raise ConnectionRefusedError("db down")

    stub_main_until_bot(monkeypatch, db_down)

    with pytest.raises(ConnectionRefusedError, match="db down"):
        await main.main()


async def rows_with_database_defaults(url: str) -> list[int]:
    """Вставки без id и с повторяющимся username; возвращает id двух скачиваний."""
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            # username не уникален: у людей без @username он пуст, а смена бывает
            await conn.execute(
                text("INSERT INTO users (id, username) VALUES (1, 'same'), (2, 'same')")
            )
            for _ in range(2):
                await conn.execute(
                    text(
                        "INSERT INTO downloads (user_id, platform, url, content_type)"
                        " VALUES (1, 'tiktok', 'https://example.invalid/v', 'video')"
                    )
                )
            result = await conn.execute(text("SELECT id FROM downloads ORDER BY id"))
            return list(result.scalars().all())
    finally:
        await engine.dispose()


async def test_upgraded_sqlite_fills_database_defaults(sqlite_url):
    await alembic(command.upgrade, alembic_config(sqlite_url), "head")

    assert await rows_with_database_defaults(sqlite_url) == [1, 2]


# ---------- PostgreSQL ----------


@pytest.fixture
async def pg_url():
    """Своя пустая база на сервере из TEST_POSTGRES_URL; удаляется после теста."""
    if not PG_URL:
        if os.environ.get("REQUIRE_POSTGRES"):
            pytest.fail("REQUIRE_POSTGRES задан, а TEST_POSTGRES_URL - нет")
        pytest.skip("TEST_POSTGRES_URL не задан: проверка на PostgreSQL - в CI")
    name = f"tt_migrations_{uuid.uuid4().hex[:12]}"
    admin = create_async_engine(PG_URL, isolation_level="AUTOCOMMIT")
    async with admin.connect() as conn:
        await conn.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        yield make_url(PG_URL).set(database=name).render_as_string(hide_password=False)
    finally:
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        await admin.dispose()


async def test_upgrade_head_on_empty_postgres_matches_models(pg_url):
    await alembic(command.upgrade, alembic_config(pg_url), "head")

    assert await schema_diff(pg_url) == []
    engine = create_async_engine(pg_url)
    try:
        await database.check_schema(engine)
    finally:
        await engine.dispose()


async def test_stamp_on_create_all_postgres_then_check_passes(pg_url):
    engine = create_async_engine(pg_url)
    async with engine.begin() as conn:
        await conn.run_sync(database.Base.metadata.create_all)
    await engine.dispose()

    await alembic(command.stamp, alembic_config(pg_url), "0001")

    assert await schema_diff(pg_url) == []
    engine = create_async_engine(pg_url)
    try:
        await database.check_schema(engine)
    finally:
        await engine.dispose()


async def test_upgraded_postgres_fills_database_defaults(pg_url):
    """downloads.id без serial на PostgreSQL: сравнение схем этого не видит, вставка - да."""
    await alembic(command.upgrade, alembic_config(pg_url), "head")

    assert await rows_with_database_defaults(pg_url) == [1, 2]
