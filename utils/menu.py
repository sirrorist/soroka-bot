"""Меню команд Telegram. Описания - из locales/.

Меню по умолчанию - русское, как и язык бота. Кто выбрал английский в /language,
тому меню ставится на его личный чат (BotCommandScopeChat): язык приложения Telegram
тут ни при чём - бот говорит на том языке, который человек выбрал.
"""
from aiogram import Bot
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
)
from loguru import logger

from config import settings
from utils.i18n import DEFAULT_LANG, t

# в группе - только с префиксом и только главное (слово владельца 2026-09-30):
# /sr_stats и /sr_premium работают, но в меню их нет
PRIVATE_COMMANDS = ["start", "dl", "stats", "premium", "sdate", "language", "help", "about"]
GROUP_COMMANDS = ["dl", "sdate", "help"]


def command_menu(lang: str, group: bool) -> list[BotCommand]:
    """Меню на языке lang: для лички или для группы (с префиксом)."""
    if group:
        p = settings.group_command_prefix
        return [
            BotCommand(command=f"{p}{name}", description=t(f"menu.group_{name}", lang=lang))
            for name in GROUP_COMMANDS
        ]
    return [BotCommand(command=name, description=t(f"menu.{name}", lang=lang)) for name in PRIVATE_COMMANDS]


async def set_default_menus(bot: Bot) -> None:
    """Меню для всех: русское в личке и в группах. Сбой - не повод не запускаться."""
    try:
        await bot.set_my_commands(command_menu(DEFAULT_LANG, False), scope=BotCommandScopeAllPrivateChats())
        await bot.set_my_commands(command_menu(DEFAULT_LANG, True), scope=BotCommandScopeAllGroupChats())
    except Exception as e:
        logger.warning(f"Command menu not set: {type(e).__name__}")


async def set_chat_menu(bot: Bot, chat_id: int, lang: str) -> None:
    """Меню личного чата на выбранном языке; русское - это меню по умолчанию."""
    scope = BotCommandScopeChat(chat_id=chat_id)
    try:
        if lang == DEFAULT_LANG:
            await bot.delete_my_commands(scope=scope)
        else:
            await bot.set_my_commands(command_menu(lang, False), scope=scope)
    except Exception as e:
        logger.warning(f"Chat menu not set: {type(e).__name__}")
