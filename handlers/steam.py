"""/sdate - сколько человек играл в Steam за последние две недели. Тексты - в locales/."""

import asyncio
import time
from html import escape

from aiogram import Dispatcher, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from loguru import logger

from config import settings
from utils.i18n import t
from utils.steam import SteamError, recent_playtime

router = Router()

TOP = 10
# ключ Steam один на всех: без кулдауна скрипт выжжет его суточную квоту
# (находка security-reviewer 2026-09-30)
COOLDOWN_S = 10
_last_call: dict[int, float] = {}
_steam_slots = asyncio.Semaphore(3)


def register_steam_handlers(dp: Dispatcher) -> None:
    """Register steam handlers."""
    dp.include_router(router)


def _hours(minutes: int) -> str:
    hours, mins = divmod(minutes, 60)
    h, m = t("units.hours"), t("units.minutes")
    return f"{hours} {h} {mins} {m}" if hours else f"{mins} {m}"


def format_playtime(stats: dict) -> str:
    """Текст ответа: итог и самые долгие игры."""
    name = escape(stats["name"])
    games = stats["games"]
    if not games:
        if not stats["public"]:
            return t("steam.closed", name=name)
        return t("steam.empty", name=name)
    total = sum(minutes for _, minutes in games)
    lines = "\n".join(
        f"{escape(title)} - {_hours(minutes)}" for title, minutes in games[:TOP]
    )
    text = t("steam.result", name=name, total=_hours(total), games=lines)
    if len(games) > TOP:
        text += t("steam.more", count=len(games) - TOP)
    return text


@router.message(Command("sdate"))
async def cmd_sdate(message: Message, command: CommandObject):
    """Handle /sdate command."""
    if not command.args:
        await message.answer(t("steam.usage", command=command.command))
        return
    if not settings.steam_api_key:
        await message.answer(t("steam.unavailable"))
        return
    user_id = message.from_user.id if message.from_user else message.chat.id
    now = time.monotonic()
    if now - _last_call.get(user_id, -COOLDOWN_S) < COOLDOWN_S:
        await message.answer(t("steam.cooldown", seconds=COOLDOWN_S))
        return
    _last_call[user_id] = now
    if len(_last_call) > 10_000:
        _last_call.clear()
    try:
        async with _steam_slots:
            stats = await recent_playtime(command.args, settings.steam_api_key)
    except SteamError as e:
        await message.answer(escape(str(e)))
        return
    except Exception as e:
        # только тип: текст может содержать URL запроса, а в нём ключ
        logger.error(f"/sdate failed: {type(e).__name__}")
        await message.answer(t("steam.failed"))
        return
    logger.info(f"/sdate ok: {len(stats['games'])} games, public={stats['public']}")
    await message.answer(format_playtime(stats))
