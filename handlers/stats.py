"""Statistics handlers."""

from html import escape

from aiogram import Dispatcher, F, Router
from aiogram.filters import Command
from sqlalchemy import func, select

from config import settings
from database import Download, User, session_scope
from utils.i18n import t
from utils.text import bar, download_word, short_name, size_parts

router = Router()

PLATFORM_NAMES = {
    "tiktok": "TikTok",
    "youtube": "YouTube",
    "instagram": "Instagram",
    "twitter": "X",
}


def register_stats_handlers(dp: Dispatcher) -> None:
    """Register stats handlers."""
    dp.include_router(router)


@router.message(Command("stats"))
@router.callback_query(F.data == "stats")
async def show_stats(message_or_query, user: User = None):
    """Show user statistics."""
    if not user:
        text = t("stats.load_failed")
        if hasattr(message_or_query, "message"):
            await message_or_query.message.edit_text(text)
            await message_or_query.answer()
        else:
            await message_or_query.answer(text)
        return

    # user detached, get user ID
    user_id = user.id

    async with session_scope() as session:
        # Load user again to get fresh data
        user_result = await session.execute(select(User).where(User.id == user_id))
        fresh_user = user_result.scalar_one()

        # Get download statistics
        result = await session.execute(
            select(
                func.count(Download.id).label("total"),
                func.sum(Download.file_size).label("total_size"),
            ).where(Download.user_id == user_id)
        )
        stats = result.first()

        # Get platform breakdown
        platform_stats = await session.execute(
            select(Download.platform, func.count(Download.id).label("count"))
            .where(Download.user_id == user_id)
            .group_by(Download.platform)
        )
        platforms = platform_stats.all()

    total_downloads = stats.total or 0
    today = fresh_user.downloads_today or 0
    premium = bool(fresh_user.is_premium)
    limit = settings.premium_user_limit if premium else settings.free_user_limit
    plan = t("common.premium_plan") if premium else t("common.free_plan")

    # Вид по образцу владельца 2026-09-30: без цитат, жирные заголовки и числа, шкала за день
    text = (
        f"{t('stats.title')}\n"
        f"{escape(short_name(fresh_user.first_name))} · {plan}\n\n"
        f"{t('stats.today')}\n"
        f"{t('stats.today_line', bar=bar(today, limit), today=today, limit=limit)}\n"
    )
    if not premium:
        text += t("stats.left", left=max(limit - today, 0))
    text += t("common.reset")

    if not total_downloads:
        text += f"\n\n{t('stats.empty')}"
    else:
        size, unit = size_parts(stats.total_size or 0)
        text += f"\n\n{t('stats.total')}\n" + t(
            "stats.total_line",
            count=total_downloads,
            word=download_word(total_downloads),
            size=size,
            unit=unit,
        )
        if platforms:
            lines = "\n".join(
                f"{PLATFORM_NAMES.get(platform, platform)} - <b>{count}</b>"
                for platform, count in sorted(platforms, key=lambda p: -p[1])
            )
            text += f"\n\n{t('stats.platforms')}\n{lines}"

    keyboard = None
    if not fresh_user.is_premium:
        from aiogram.utils.keyboard import InlineKeyboardBuilder

        keyboard = InlineKeyboardBuilder()
        keyboard.button(text=t("common.premium_button"), callback_data="premium")

    if hasattr(message_or_query, "message"):
        await message_or_query.message.edit_text(
            text, reply_markup=keyboard.as_markup() if keyboard else None
        )
        await message_or_query.answer()
    else:
        await message_or_query.answer(
            text, reply_markup=keyboard.as_markup() if keyboard else None
        )
