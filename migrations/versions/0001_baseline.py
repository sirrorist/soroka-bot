"""Базовая схема: четыре таблицы, какими их создаёт create_all на 2026-10-01.

Боевая база создана create_all задолго до миграций - на ней эта ревизия не
выполняется, а ставится отметкой: `alembic stamp 0001`. На пустой базе (стенд,
новый сервер) `alembic upgrade head` создаёт таблицы отсюда.

Столбцы - как в моделях database.py на момент ревизии, а не импортом моделей:
модели дальше меняются, ревизия - нет. Совпадение с create_all проверяет
tests/test_migrations.py.

Revision ID: 0001
Revises:
Create Date: 2026-10-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("username", sa.String(255), nullable=True),
        sa.Column("first_name", sa.String(255), nullable=True),
        sa.Column("last_name", sa.String(255), nullable=True),
        sa.Column("is_premium", sa.Boolean()),
        sa.Column("downloads_today", sa.Integer()),
        sa.Column("total_downloads", sa.Integer()),
        sa.Column("created_at", sa.DateTime()),
        sa.Column("last_activity", sa.DateTime()),
    )
    op.create_table(
        "downloads",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "user_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False
        ),
        sa.Column("platform", sa.String(50), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("content_type", sa.String(50), nullable=False),
        sa.Column("file_size", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(20)),
        sa.Column("created_at", sa.DateTime()),
    )
    op.create_table(
        "user_settings",
        sa.Column("user_id", sa.BigInteger(), primary_key=True),
        sa.Column("lang", sa.String(8), nullable=True),
        sa.Column("updated_at", sa.DateTime()),
    )
    op.create_table(
        "chats",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("title", sa.String(255), nullable=True),
        sa.Column("is_premium", sa.Boolean()),
        sa.Column("premium_until", sa.DateTime(), nullable=True),
        sa.Column("day", sa.Date(), nullable=True),
        sa.Column("downloads_today", sa.Integer()),
        sa.Column("bytes_today", sa.BigInteger()),
        sa.Column("total_downloads", sa.Integer()),
        sa.Column("created_at", sa.DateTime()),
    )


def downgrade() -> None:
    # Откат базовой ревизии - это DROP всех таблиц. На боевой базе он стёр бы всех
    # пользователей и историю, поэтому его нет: снести схему можно только руками.
    raise RuntimeError("Откат базовой ревизии 0001 запрещён: он удалил бы все данные")
