"""Database models and initialization."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    inspect,
    text,
)
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import declarative_base, relationship

from config import settings
from utils.clock import utcnow

Base = declarative_base()


class User(Base):
    """User model."""

    __tablename__ = "users"

    id = Column(BigInteger, primary_key=True)
    username = Column(String(255), nullable=True)
    first_name = Column(String(255), nullable=True)
    last_name = Column(String(255), nullable=True)
    is_premium = Column(Boolean, default=False)
    downloads_today = Column(Integer, default=0)
    total_downloads = Column(Integer, default=0)
    created_at = Column(DateTime, default=utcnow)
    last_activity = Column(DateTime, default=utcnow)

    # Relationships
    downloads = relationship("Download", back_populates="user")


class Download(Base):
    """Download history model."""

    __tablename__ = "downloads"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey("users.id"), nullable=False)
    platform = Column(String(50), nullable=False)
    url = Column(Text, nullable=False)
    content_type = Column(String(50), nullable=False)  # video, photo, text
    file_size = Column(Integer, nullable=True)
    status = Column(String(20), default="completed")  # completed, failed, processing
    created_at = Column(DateTime, default=utcnow)

    # Relationships
    user = relationship("User", back_populates="downloads")


class UserSetting(Base):
    """Настройки пользователя, которые он выбирает сам. Отдельная таблица, а не
    столбцы users: заведена до миграций, когда create_all добавлял таблицы, но не
    столбцы. Схема с 2026-10-01 - ревизии migrations/."""

    __tablename__ = "user_settings"

    user_id = Column(BigInteger, primary_key=True)
    lang = Column(String(8), nullable=True)  # пусто - язык по умолчанию (ru)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)


class Chat(Base):
    """Группа, где работает бот: лимит и Premium - на чат, а не на участника.

    Новая таблица, а не новые столбцы: заведена до миграций, когда create_all на
    боевой базе создавал недостающие таблицы, но не менял существующие. Схема с
    2026-10-01 - ревизии migrations/.
    """

    __tablename__ = "chats"

    id = Column(
        BigInteger, primary_key=True
    )  # id чата в Telegram, у групп отрицательный
    title = Column(String(255), nullable=True)
    is_premium = Column(Boolean, default=False)
    # пусто при is_premium - бессрочно; срок понадобится оплате (этап 11)
    premium_until = Column(DateTime, nullable=True)
    # день, к которому относятся счётчики: наступил другой - они обнуляются
    day = Column(Date, nullable=True)
    downloads_today = Column(Integer, default=0)
    bytes_today = Column(BigInteger, default=0)
    total_downloads = Column(Integer, default=0)
    created_at = Column(DateTime, default=utcnow)


# Database engine and session
engine = create_async_engine(settings.database_url, echo=settings.debug, future=True)

async_session_maker = async_sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False
)


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Сессия на блок `async with`: закрывается на выходе из него, в том числе по return.

    Раньше здесь был асинхронный генератор get_db под `async for ... break`. break
    генератор не закрывает: сессия держала соединение, пока генератор не подберёт
    сборщик мусора, а на закрытом цикле событий не закрывалась вовсе (плавающий
    ResourceWarning от aiosqlite в CI, 2026-10-01).
    """
    async with async_session_maker() as session:
        yield session


# Ревизия Alembic, под которую написан этот код: последняя в migrations/versions/.
# Константа, а не чтение каталога ревизий: импорт alembic стоил бы боту ~7 МБ памяти
# (проверено 2026-10-01). Что она совпадает с последней ревизией, следит
# tests/test_migrations.py - новая ревизия без правки здесь краснеет в CI.
SCHEMA_REVISION = "0001"


class SchemaNotReady(RuntimeError):
    """База не на той ревизии Alembic, которую ждёт код."""


def _db_revision(sync_conn) -> str | None:
    """Отметка Alembic в базе; None - таблицы alembic_version нет или она пуста."""
    if not inspect(sync_conn).has_table("alembic_version"):
        return None
    return sync_conn.execute(text("SELECT version_num FROM alembic_version")).scalar()


async def check_schema(bind: AsyncEngine | None = None) -> None:
    """Бот схему не меняет, а проверяет: база должна быть на последней ревизии.

    До 2026-10-01 здесь был create_all на каждом старте: он создаёт недостающие
    таблицы, но не столбцы, и новый код на старой схеме падал бы посреди работы.
    Теперь схему меняет только `alembic upgrade head` (до запуска бота), а бот на
    неподходящей базе не стартует и говорит, что сделать.
    """
    head = SCHEMA_REVISION
    async with (bind or engine).connect() as conn:
        current = await conn.run_sync(_db_revision)
    if current == head:
        return
    if current is None:
        raise SchemaNotReady(
            f"У базы нет отметки Alembic, код ждёт ревизию {head}. Пустая база - "
            "`alembic upgrade head`; база, созданная до миграций, - сверить схему и "
            "`alembic stamp 0001`, затем `alembic upgrade head`"
        )
    raise SchemaNotReady(
        f"База на ревизии {current}, код ждёт {head}: `alembic upgrade head`"
        " (если ревизия базы новее кода - запущен старый образ)"
    )
