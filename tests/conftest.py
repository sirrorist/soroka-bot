"""Общие фикстуры. Сети и настоящей базы в тестах нет."""

import os

import pytest
import pytest_asyncio

# Импорт database.py создаёт движок, ему нужны эти переменные; в CI и в командах
# из commands.md они заданы, здесь - запасные значения для запуска "голым" pytest.
os.environ.setdefault("BOT_TOKEN", "0:x")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://t")
# Префикс групповых команд читается при импорте handlers/group.py; тесты маршрутизации
# ждут "sr_", поэтому чужое значение из окружения разработчика им не подставляется
os.environ.pop("GROUP_COMMAND_PREFIX", None)

from config import settings  # noqa: E402

_PROXY_VARS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
)


@pytest.fixture(autouse=True)
def _no_proxy_env(monkeypatch):
    """Прокси из окружения разработчика не должен ни скрывать запросы, ни выпускать их наружу."""
    for name in _PROXY_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def workroot(tmp_path, monkeypatch):
    """TEMP_DIR во временный каталог: боевой ./temp в клоне не трогаем."""
    root = tmp_path / "work"
    monkeypatch.setattr(settings, "temp_dir", str(root))
    return root


@pytest.fixture(autouse=True)
def _reset_lang():
    """Язык ответа - contextvar: тест, поставивший en, не должен оставить его соседу."""
    from utils.i18n import DEFAULT_LANG, _lang

    token = _lang.set(DEFAULT_LANG)
    yield
    _lang.reset(token)


# Корневой pytest.ini начинается с [tool:pytest] - для pytest.ini это неверный заголовок,
# поэтому asyncio_mode=auto из него не действует (проверено): корутины-тесты помечаются
# @pytest.mark.asyncio вручную, а async-фикстуры объявляются через pytest_asyncio.
@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    """Настоящая SQLite (aiosqlite) во временном файле вместо PostgreSQL.

    Подменяется async_session_maker в database.py: session_scope() берёт его из модуля
    в момент вызова, поэтому все обработчики и middleware ходят в тестовую базу. Боевой
    движок (postgresql+asyncpg://test) не открывает ни одного соединения. Кэш языка
    чистится.

    После теста ни одна сессия не должна остаться незакрытой. Две проверки, обе нужны:
    - пул: ни одно соединение не занято. dispose() закрывает только вернувшиеся в пул,
      а брошенное сборщик мусора подбирал потом в чужом тесте - плавающий
      ResourceWarning в CI 2026-10-01 (break из `async for` по генератору сессий);
    - сессии: у каждой созданной вызван close(). Пул один не ловит утечку на пути,
      который кончается commit: commit возвращает соединение в пул, и незакрытая
      сессия выглядит как закрытая. Утечка в таком месте всплывает при первом же
      пути без commit (ранний return, чтение), поэтому закрытие проверяется само.
    """
    from sqlalchemy.ext.asyncio import (
        AsyncSession,
        async_sessionmaker,
        create_async_engine,
    )

    import database
    from utils import lang_store

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(database.Base.metadata.create_all)
    created: list = []
    closed: set[int] = set()

    class TrackedSession(AsyncSession):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            created.append(self)

        async def close(self):
            closed.add(id(self))
            await super().close()

    maker = async_sessionmaker(engine, class_=TrackedSession, expire_on_commit=False)
    monkeypatch.setattr(database, "async_session_maker", maker)
    monkeypatch.setattr(lang_store, "_cache", {})
    yield maker
    checked_out = engine.pool.checkedout()
    await engine.dispose()
    unclosed = [s for s in created if id(s) not in closed]
    assert checked_out == 0, f"тест оставил занятых соединений: {checked_out}"
    assert not unclosed, (
        f"тест оставил незакрытых сессий: {len(unclosed)} из {len(created)}"
    )
