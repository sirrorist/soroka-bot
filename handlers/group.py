"""Бот в группах: только команды с префиксом, лимит и Premium - на чат.

Префикс (settings.group_command_prefix, по умолчанию sr_) - чтобы не отвечать на
/start, /help и /stats, которые в той же группе адресованы другим ботам.

Ответ уходит в тот же топик сам: Message.answer в aiogram подставляет
message_thread_id, если сообщение пришло из топика.
"""

import asyncio
from html import escape

from aiogram import Dispatcher, F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message
from aiogram.types import Chat as TgChat
from loguru import logger
from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError

from config import settings
from database import Chat, User, session_scope
from downloaders.common import extract_url
from handlers import download as dl
from handlers import steam
from handlers.premium import prices, request_keyboard
from utils.clock import utcnow
from utils.i18n import t
from utils.text import bar, download_word, downloads_word, short_name, size_text

GROUPS = {ChatType.GROUP, ChatType.SUPERGROUP}
MB = 1024 * 1024
P = settings.group_command_prefix

# Два служебных сообщения о переезде aiogram обрабатывает одновременно: без замка оба
# находили старую строку и переносили счётчики дважды (проверено на стенде 2026-09-30)
_migrate_lock = asyncio.Lock()

router = Router()
router.message.filter(F.chat.type.in_(GROUPS))
router.callback_query.filter(F.message.chat.type.in_(GROUPS))


def register_group_handlers(dp: Dispatcher) -> None:
    """Register group handlers."""
    dp.include_router(router)


def chat_is_premium(chat: Chat) -> bool:
    """Premium чата: флаг и срок, если он задан."""
    if not chat.is_premium:
        return False
    return chat.premium_until is None or chat.premium_until > utcnow()


def chat_limits(premium: bool) -> tuple[int, int]:
    """Скачиваний и байт в день для чата."""
    if premium:
        return settings.premium_user_limit, settings.group_premium_daily_mb * MB
    return settings.group_free_limit, settings.group_free_daily_mb * MB


async def get_chat(tg_chat: TgChat) -> Chat:
    """Строка чата: создаётся при первой команде, счётчики - за сегодня."""
    today = utcnow().date()
    chat = None
    async with session_scope() as session:
        chat = await session.get(Chat, tg_chat.id)
        if chat is None:
            session.add(Chat(id=tg_chat.id, title=tg_chat.title, day=today))
            try:
                await session.commit()
            except IntegrityError:
                # две первые команды разом: строку уже создал соседний запрос
                await session.rollback()
        elif chat.title != tg_chat.title:
            chat.title = tg_chat.title
            await session.commit()
        await _roll_day(session, tg_chat.id)
        await session.commit()
        chat = await session.get(Chat, tg_chat.id, populate_existing=True)
    return chat


async def _roll_day(session, chat_id: int) -> None:
    """Наступил другой день - счётчики с нуля. Условие по day делает это один раз."""
    today = utcnow().date()
    await session.execute(
        update(Chat)
        .where(Chat.id == chat_id, or_(Chat.day.is_(None), Chat.day != today))
        .values(day=today, downloads_today=0, bytes_today=0)
    )


async def migrate_chat(old_id: int, new_id: int, title: str | None) -> None:
    """Группа стала супергруппой (например, включили темы) - у неё новый id.

    Premium и счётчики переезжают на новый id, иначе оплаченный Premium пропал бы.
    Telegram шлёт два служебных сообщения (в старый и в новый чат) - второе
    ничего не находит и ничего не делает.
    """
    async with _migrate_lock:
        await _migrate_chat(old_id, new_id, title)


async def _migrate_chat(old_id: int, new_id: int, title: str | None) -> None:
    async with session_scope() as session:
        old = await session.get(Chat, old_id)
        if old is None:
            return
        new = await session.get(Chat, new_id)
        if new is None:
            session.add(
                Chat(
                    id=new_id,
                    title=title or old.title,
                    is_premium=old.is_premium,
                    premium_until=old.premium_until,
                    day=old.day,
                    downloads_today=old.downloads_today,
                    bytes_today=old.bytes_today,
                    total_downloads=old.total_downloads,
                    created_at=old.created_at,
                )
            )
        else:
            if chat_is_premium(old) and not chat_is_premium(new):
                new.is_premium = True
                new.premium_until = old.premium_until
            new.total_downloads = (new.total_downloads or 0) + (
                old.total_downloads or 0
            )
        await session.delete(old)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
        logger.info(f"Chat {old_id} migrated to {new_id}")


@router.message(F.migrate_to_chat_id)
async def on_migrate_to(message: Message):
    """Служебное сообщение в старой группе: она переехала в супергруппу."""
    logger.info(
        f"Migrate service message: {message.chat.id} -> {message.migrate_to_chat_id}"
    )
    await migrate_chat(message.chat.id, message.migrate_to_chat_id, None)


@router.message(F.migrate_from_chat_id)
async def on_migrate_from(message: Message):
    """Служебное сообщение в новой супергруппе: откуда она переехала."""
    logger.info(
        f"Migrate service message: {message.migrate_from_chat_id} -> {message.chat.id}"
    )
    await migrate_chat(
        message.migrate_from_chat_id, message.chat.id, message.chat.title
    )


async def _reserve_chat(chat_id: int, limit: int, daily_bytes: int) -> bool:
    """Занять скачивание из лимита чата до загрузки. False - лимит исчерпан."""
    downloads_today = func.coalesce(Chat.downloads_today, 0)
    bytes_today = func.coalesce(Chat.bytes_today, 0)
    reserved = False
    async with session_scope() as session:
        await _roll_day(session, chat_id)
        result = await session.execute(
            update(Chat)
            .where(
                Chat.id == chat_id, downloads_today < limit, bytes_today < daily_bytes
            )
            .values(downloads_today=downloads_today + 1)
        )
        await session.commit()
        reserved = result.rowcount == 1
    return reserved


async def _release_chat(chat_id: int) -> None:
    """Вернуть занятое скачивание: файл так и не ушёл в чат."""
    async with session_scope() as session:
        await session.execute(
            update(Chat)
            .where(Chat.id == chat_id, Chat.downloads_today > 0)
            .values(downloads_today=Chat.downloads_today - 1)
        )
        await session.commit()


async def _chat_fits(chat_id: int, daily_bytes: int, size: int) -> str | None:
    """Отказ, если скачанный файл не влезает в остаток дневного объёма чата."""
    used = 0
    async with session_scope() as session:
        used = (
            await session.scalar(
                select(func.coalesce(Chat.bytes_today, 0)).where(Chat.id == chat_id)
            )
            or 0
        )
    if used + size <= daily_bytes:
        return None
    return t(
        "group.not_fits",
        size=size_text(size),
        left=size_text(max(daily_bytes - used, 0)),
    )


async def _chat_delivered(chat_id: int, size: int) -> None:
    """Учесть отправленный файл: объём за день и всего скачиваний."""
    async with session_scope() as session:
        await session.execute(
            update(Chat)
            .where(Chat.id == chat_id)
            .values(
                bytes_today=func.coalesce(Chat.bytes_today, 0) + size,
                total_downloads=func.coalesce(Chat.total_downloads, 0) + 1,
            )
        )
        await session.commit()


def _link_from(message: Message, command: CommandObject) -> tuple[str, str] | None:
    """Ссылка из аргумента команды или из сообщения, на которое ответили командой."""
    if command.args:
        return extract_url(command.args)
    replied = message.reply_to_message
    if replied:
        return extract_url(replied.text or replied.caption or "")
    return None


@router.message(Command(f"{P}dl"))
async def group_download(message: Message, command: CommandObject, user: User = None):
    """Скачать по команде в группе; лимит тратит чат."""
    if not user:
        return
    found = _link_from(message, command)
    if not found:
        await message.answer(t("group.dl_usage", p=P))
        return
    platform, url = found

    chat = await get_chat(message.chat)
    chat_id = chat.id
    premium = chat_is_premium(chat)
    limit, daily_bytes = chat_limits(premium)
    limit_text = t(
        "group.limit", downloads=downloads_word(limit), size=size_text(daily_bytes)
    )
    if not premium:
        limit_text += t("group.limit_upsell", p=P)

    # анонимный администратор приходит от общего GroupAnonymousBot с одним id на все
    # группы: его "одна загрузка за раз" - на чат (находка security-reviewer 2026-09-30).
    # id чатов отрицательные, людей - положительные, поэтому ключи не пересекаются
    slot_key = message.sender_chat.id if message.sender_chat else user.id
    await dl.deliver(
        message,
        user_id=user.id,
        slot_key=slot_key,
        premium=premium,
        platform=platform,
        url=url,
        reserve=lambda: _reserve_chat(chat_id, limit, daily_bytes),
        release=lambda: _release_chat(chat_id),
        limit_text=limit_text,
        fits=lambda size: _chat_fits(chat_id, daily_bytes, size),
        on_delivered=lambda size: _chat_delivered(chat_id, size),
    )


@router.message(Command(f"{P}sdate"))
async def group_sdate(message: Message, command: CommandObject):
    """/sdate в группе - то же, что в личке."""
    await steam.cmd_sdate(message, command)


@router.message(Command(f"{P}help"))
@router.callback_query(F.data.in_({"help", "download_menu"}))
async def group_help(event: Message | CallbackQuery):
    """Справка для группы."""
    if isinstance(event, CallbackQuery):
        await event.message.edit_text(t("group.help", p=P))
        await event.answer()
    else:
        await event.answer(t("group.help", p=P))


@router.message(Command(f"{P}stats"))
@router.callback_query(F.data == "stats")
async def group_stats(event: Message | CallbackQuery):
    """Лимит и счётчики чата."""
    tg_chat = event.message.chat if isinstance(event, CallbackQuery) else event.chat
    chat = await get_chat(tg_chat)
    premium = chat_is_premium(chat)
    limit, daily_bytes = chat_limits(premium)
    today, used = chat.downloads_today or 0, chat.bytes_today or 0
    total = chat.total_downloads or 0
    plan = t("common.premium_plan") if premium else t("common.free_plan")
    text = (
        f"{t('group.stats_title')}\n"
        f"{escape(short_name(tg_chat.title))} · {plan}\n\n"
        f"{t('stats.today')}\n"
        + t(
            "group.stats_downloads",
            bar=bar(today, limit),
            today=today,
            limit=limit,
            word=download_word(limit),
        )
        + "\n"
        + t(
            "group.stats_bytes",
            bar=bar(used, daily_bytes),
            used=size_text(used),
            daily=size_text(daily_bytes),
        )
        + "\n"
        + f"{t('common.reset')}\n\n{t('stats.total')}\n"
        + t("group.stats_total_line", count=total, word=download_word(total))
    )
    if isinstance(event, CallbackQuery):
        await event.message.edit_text(text)
        await event.answer()
    else:
        await event.answer(text)


@router.message(Command(f"{P}premium"))
@router.callback_query(F.data == "premium")
async def group_premium(event: Message | CallbackQuery):
    """Premium чата: отдельный от Premium участников."""
    tg_chat = event.message.chat if isinstance(event, CallbackQuery) else event.chat
    chat = await get_chat(tg_chat)
    keyboard = None
    if chat_is_premium(chat):
        text = t("group.premium_active")
        if chat.premium_until:
            text += t("group.premium_until", date=f"{chat.premium_until:%d.%m.%Y}")
    else:
        free_limit, free_bytes = chat_limits(False)
        limit, daily_bytes = chat_limits(True)
        text = t(
            "group.premium_offer",
            free_downloads=downloads_word(free_limit),
            free_size=size_text(free_bytes),
            downloads=downloads_word(limit),
            size=size_text(daily_bytes),
            prices=prices(
                settings.chat_premium_price_month, settings.chat_premium_price_year
            ),
        )
        keyboard = request_keyboard()
    if isinstance(event, CallbackQuery):
        await event.message.edit_text(text, reply_markup=keyboard)
        await event.answer()
    else:
        await event.answer(text, reply_markup=keyboard)
