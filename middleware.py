"""Middleware for the bot."""

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject
from aiogram.types import User as TgUser
from loguru import logger
from sqlalchemy import select

from config import settings
from database import User, session_scope
from utils.clock import utcnow
from utils.i18n import DEFAULT_LANG, set_lang
from utils.lang_store import get_user_lang


class UserMiddleware(BaseMiddleware):
    """Middleware to track and update user information."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        """Process user data."""
        tg_user: TgUser = data.get("event_from_user")
        if not tg_user:
            return await handler(event, data)

        async with session_scope() as session:
            # Get or create user
            result = await session.execute(select(User).where(User.id == tg_user.id))
            user = result.scalar_one_or_none()

            if not user:
                user = User(
                    id=tg_user.id,
                    username=tg_user.username,
                    first_name=tg_user.first_name,
                    last_name=tg_user.last_name,
                    is_premium=tg_user.is_premium or False,
                )
                session.add(user)
                await session.commit()
            else:
                # Update user info
                user.username = tg_user.username
                user.first_name = tg_user.first_name
                user.last_name = tg_user.last_name
                user.is_premium = tg_user.is_premium or user.is_premium
                now = utcnow()
                # Сброс дневного счётчика - здесь, до записи last_activity: раньше
                # RateLimitMiddleware сверял уже обновлённую дату и не сбрасывал никогда
                if user.last_activity and user.last_activity.date() < now.date():
                    user.downloads_today = 0
                user.last_activity = now
                await session.commit()

            # Make attributes persistent before session closes
            _ = user.downloads_today  # Trigger lazy load
            _ = user.total_downloads  # Trigger lazy load
            _ = user.is_premium  # Trigger lazy load

            data["user"] = user

        return await handler(event, data)


class RateLimitMiddleware(BaseMiddleware):
    """Middleware to enforce rate limiting."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        """Check rate limits."""
        user: User = data.get("user")
        if not user:
            return await handler(event, data)

        # Check limit; дневной счётчик уже сброшен в UserMiddleware
        limit = (
            settings.premium_user_limit if user.is_premium else settings.free_user_limit
        )

        if user.downloads_today >= limit:
            data["rate_limited"] = True
        else:
            data["rate_limited"] = False

        return await handler(event, data)


class CommandLogMiddleware(BaseMiddleware):
    """Пишет в лог каждую команду: какая и в каком чате. Аргументы не пишутся."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        text = getattr(event, "text", None) or ""
        if text.startswith("/"):
            command = text.split(maxsplit=1)[0][:40]
            chat = getattr(event, "chat", None)
            logger.info(
                f"Command {command} in {chat.type if chat else '?'} chat {chat.id if chat else '?'}"
            )
        return await handler(event, data)


class LangMiddleware(BaseMiddleware):
    """Язык ответа - тот, что человек выбрал в /language; по умолчанию русский.

    Не по language_code: язык приложения Telegram не говорит, на каком языке
    человеку удобнее читать бота (слово владельца 2026-09-30).
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        tg_user = data.get("event_from_user")
        set_lang(await get_user_lang(tg_user.id) if tg_user else DEFAULT_LANG)
        return await handler(event, data)
