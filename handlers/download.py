"""Download handlers."""
import asyncio
from collections.abc import Awaitable, Callable
from html import escape
from aiogram import Router, F, Dispatcher
from aiogram.enums import ChatType
from aiogram.types import Message, FSInputFile
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, select, update
from loguru import logger

from database import get_db, User, Download
from downloaders import (
    download_tiktok,
    download_youtube,
    download_instagram,
    download_twitter,
)
from downloaders.common import extract_url, max_send_mb, remove_workdir
from config import settings
from utils.i18n import t

router = Router()

DOWNLOADERS = {
    "tiktok": download_tiktok,
    "youtube": download_youtube,
    "instagram": download_instagram,
    "twitter": download_twitter,
}

# Сколько скачиваний идёт одновременно на весь бот и кто сейчас качает:
# одному пользователю - одна загрузка за раз
_download_slots = asyncio.Semaphore(settings.max_parallel_downloads)
_active_users: set[int] = set()


async def _reserve_download(user_id: int, limit: int) -> bool:
    """Занять одно скачивание из лимита до начала загрузки. False - лимит исчерпан."""
    downloads_today = func.coalesce(User.downloads_today, 0)
    reserved = False
    # break, а не return: иначе генератор get_db не закроется и сессия повиснет
    async for session in get_db():
        result = await session.execute(
            update(User)
            .where(User.id == user_id, downloads_today < limit)
            .values(downloads_today=downloads_today + 1)
        )
        await session.commit()
        reserved = result.rowcount == 1
        break
    return reserved


async def _release_download(user_id: int) -> None:
    """Вернуть занятое скачивание, если файл так и не ушёл пользователю."""
    async for session in get_db():
        await session.execute(
            update(User)
            .where(User.id == user_id, User.downloads_today > 0)
            .values(downloads_today=User.downloads_today - 1)
        )
        await session.commit()
        break


# Названия, которые загрузчики подставляют, когда у ролика своего нет
_PLACEHOLDER_TITLES = {"TikTok Video", "YouTube Content", "Instagram Content", "Twitter Content"}
_PLATFORM_NAMES = {"tiktok": "TikTok", "youtube": "YouTube", "instagram": "Instagram", "twitter": "X"}
# подпись к файлу в Telegram - до 1024 символов; название ролика бывает длинным
_TITLE_MAX = 200


def _caption(title: str | None, platform: str, url: str) -> str:
    """Подпись к файлу: название ролика ссылкой на оригинал (слово владельца 2026-09-30)."""
    title = " ".join((title or "").split())
    if not title or title in _PLACEHOLDER_TITLES:
        title = t("download.caption_fallback", platform=_PLATFORM_NAMES.get(platform, platform))
    elif len(title) > _TITLE_MAX:
        title = title[:_TITLE_MAX].rstrip() + "..."
    return f'<a href="{escape(url, quote=True)}">{escape(title)}</a>'


def _not_spent() -> str:
    """Неудача лимит не тратит - decisions/0001; release() в finally у deliver."""
    return t("common.not_spent")


def _limit_keyboard():
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text=t("common.premium_button"), callback_data="premium")
    return keyboard.as_markup()


def limit_text(premium: bool) -> str:
    """Лимит исчерпан. Счётчик обнуляется в полночь по UTC (UserMiddleware).

    У Premium свой текст: без "7 из 7" и без предложения купить то, что уже куплено.
    """
    if premium:
        return t("download.limit_premium", premium=settings.premium_user_limit)
    return t("download.limit_free", free=settings.free_user_limit, premium=settings.premium_user_limit)


def register_download_handlers(dp: Dispatcher) -> None:
    """Register download handlers."""
    dp.include_router(router)


@router.message(Command("download"))
@router.callback_query(F.data == "download_menu")
async def download_menu(message_or_query):
    """Show download menu."""
    text = t("download.menu", limits=t("common.limits_block", max_mb=max_send_mb()))

    if hasattr(message_or_query, "message"):
        await message_or_query.message.edit_text(text)
        await message_or_query.answer()
    else:
        await message_or_query.answer(text)


@router.message(F.chat.type == ChatType.PRIVATE, Command("dl"))
async def private_dl(message: Message, user: User = None, rate_limited: bool = False):
    """/dl ссылка в личке - как в группе, но лимит личный."""
    await handle_url(message, user=user, rate_limited=rate_limited)


# Только личка и только не команда: иначе этот обработчик, стоящий на любом тексте,
# забирает /stats и /premium, а в группе реагирует на каждое сообщение
@router.message(F.chat.type == ChatType.PRIVATE, F.text, ~F.text.startswith("/"))
async def handle_url(message: Message, user: User = None, rate_limited: bool = False):
    """Handle URL message."""
    if not user:
        return  # User not found, skip

    # Сохраняем user_id и статус premium, потому что объект user detached
    user_id = user.id
    user_is_premium = user.is_premium

    if rate_limited:
        await message.answer(limit_text(user_is_premium), reply_markup=None if user_is_premium else _limit_keyboard())
        return

    # В загрузчик уходит только ссылка на известную площадку, а не весь текст
    found = extract_url(message.text)
    if not found:
        return  # Not a URL, ignore
    platform, url = found

    limit = settings.premium_user_limit if user_is_premium else settings.free_user_limit
    # лямбды, а не сами функции: имена ищутся в модуле в момент вызова
    await deliver(
        message,
        user_id=user_id,
        premium=user_is_premium,
        platform=platform,
        url=url,
        reserve=lambda: _reserve_download(user_id, limit),
        release=lambda: _release_download(user_id),
        limit_text=limit_text(user_is_premium),
    )


async def deliver(
    message: Message,
    *,
    user_id: int,
    premium: bool,
    platform: str,
    url: str,
    reserve: Callable[[], Awaitable[bool]],
    release: Callable[[], Awaitable[None]],
    limit_text: str,
    fits: Callable[[int], Awaitable[str | None]] | None = None,
    on_delivered: Callable[[int], Awaitable[None]] | None = None,
    slot_key: int | None = None,
) -> None:
    """Скачать и отправить файл - общий путь для лички и группы.

    Чей лимит тратится, решает вызывающий: reserve занимает скачивание до загрузки,
    release возвращает его, если файл не ушёл. fits - отказ по размеру уже
    скачанного файла (текст) или None; on_delivered - учёт после отправки.
    slot_key - кто занимает "одну загрузку за раз"; по умолчанию user_id.
    """
    download = DOWNLOADERS.get(platform)
    if not download:
        await message.answer(t("download.unsupported"))
        return

    user_is_premium = premium

    slot = user_id if slot_key is None else slot_key
    # проверка и отметка - без await между ними, иначе две ссылки подряд пройдут обе
    if slot in _active_users:
        await message.answer(t("download.busy"))
        return
    _active_users.add(slot)

    try:
        reserved = await reserve()
    except BaseException:
        _active_users.discard(slot)
        raise
    if not reserved:
        _active_users.discard(slot)
        await message.answer(limit_text, reply_markup=None if premium else _limit_keyboard())
        return

    delivered = False
    result = None
    processing_msg = None

    try:
        # Send processing message with premium status
        processing_msg = await message.answer(t("download.working"))

        async with _download_slots:
            result = await download(url)

        if not result.get("success"):
            error = str(result.get("error") or t("download.unknown_reason"))
            # текст ошибки приходит от yt-dlp и сайта - в HTML только экранированным
            await processing_msg.edit_text(f"{t('download.failed', reason=escape(error[:300]))}\n{_not_spent()}")
            logger.info(
                f"Download failed for user {user_id} (premium: {user_is_premium}) "
                f"from {platform}: {error}"
            )
            return

        # Send file
        file_path = result["file_path"]
        file_size_mb = result.get("file_size", 0) / (1024 * 1024)

        if file_size_mb > settings.max_file_size_mb:
            await processing_msg.edit_text(
                t("download.too_big", size=f"{file_size_mb:.0f}", max_mb=max_send_mb())
                + f"\n{_not_spent()}"
            )
            return

        if fits:
            refusal = await fits(result.get("file_size") or 0)
            if refusal:
                await processing_msg.edit_text(f"{refusal}\n{_not_spent()}")
                return

        file = FSInputFile(file_path)
        caption = _caption(result.get("title"), platform, url)

        if result["content_type"] == "video":
            await message.answer_video(file, caption=caption)
        elif result["content_type"] == "photo":
            await message.answer_photo(file, caption=caption)
        else:
            await message.answer_document(file, caption=caption)

        delivered = True
        await processing_msg.delete()
        processing_msg = None  # удалено - в except его уже не править

        # Update user stats. downloads_today уже увеличен в _reserve_download
        async for session in get_db():
            # Refresh user to get current values
            db_result = await session.execute(select(User).where(User.id == user_id))
            fresh_user = db_result.scalar_one_or_none()

            if fresh_user:
                # fresh_user attached into current session
                fresh_user.total_downloads = (fresh_user.total_downloads or 0) + 1

                # Save download history
                session.add(Download(
                    user_id=user_id,
                    platform=platform,
                    url=url,
                    content_type=result["content_type"],
                    file_size=result.get("file_size"),
                    status="completed",
                ))
                await session.commit()
                
                # Log успешного скачивания с указанием premium статуса
                logger.info(
                    f"Download completed for user {user_id} (premium: {user_is_premium}): "
                    f"{platform} - {file_size_mb:.1f}MB"
                )
            break

        if on_delivered:
            await on_delivered(result.get("file_size") or 0)

    except Exception:
        # opt(exception=True), а не exc_info: loguru прогнал бы текст через str.format
        logger.opt(exception=True).error(
            f"Download error for user {user_id} (premium: {user_is_premium})"
        )
        if processing_msg:
            await processing_msg.edit_text(f"{t('download.crashed')}\n{_not_spent()}")

    finally:
        _active_users.discard(slot)
        # файл уже у пользователя или не нужен - каталог запроса удаляется в любом случае
        if result and result.get("file_path"):
            remove_workdir(result["file_path"])
        if not delivered:
            await release()
