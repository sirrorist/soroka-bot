"""Premium: описание и заявка администратору, пока в боте нет оплаты (этап 11).

Тексты - в locales/; сообщение владельцу - всегда по-русски.
"""
import time
from html import escape

from aiogram import Dispatcher, F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.types import CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from loguru import logger

from config import settings
from utils.i18n import t

router = Router()

# Одна заявка от человека в час: иначе кнопкой можно засыпать администратора
REQUEST_COOLDOWN_S = 3600
_last_request: dict[int, float] = {}


def register_premium_handlers(dp: Dispatcher) -> None:
    """Register premium handlers."""
    dp.include_router(router)


def prices(month: int, year: int) -> str:
    """Блок цен: личный Premium и Premium чата стоят по-разному."""
    return t("premium.prices", month=month, year=year)


def request_keyboard():
    """Кнопка заявки под описанием Premium - в личке и в группе."""
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text=t("premium.request_button"), callback_data="premium_request")
    return keyboard.as_markup()


@router.message(Command("premium"))
@router.callback_query(F.data == "premium")
async def show_premium(message_or_query, user=None):
    """Show premium information."""
    is_premium = user.is_premium if user and hasattr(user, 'is_premium') else False

    if is_premium:
        text = t("premium.active", premium=settings.premium_user_limit)
        keyboard = None
    else:
        text = t(
            "premium.offer",
            premium=settings.premium_user_limit,
            free=settings.free_user_limit,
            prices=prices(settings.premium_price_month, settings.premium_price_year),
        )
        keyboard = request_keyboard()

    if hasattr(message_or_query, "message"):
        await message_or_query.message.edit_text(text, reply_markup=keyboard)
        await message_or_query.answer()
    else:
        await message_or_query.answer(text, reply_markup=keyboard)


@router.callback_query(F.data == "premium_request")
async def premium_request(callback: CallbackQuery):
    """Предупредить, что ник уйдёт администратору, и спросить согласия."""
    text = t("premium.ask")
    if not callback.from_user.username:
        text += t("premium.no_username")
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text=t("premium.yes_button"), callback_data="premium_confirm")
    keyboard.button(text=t("premium.cancel_button"), callback_data="cancel")
    keyboard.adjust(2)
    await callback.message.edit_text(text, reply_markup=keyboard.as_markup())
    await callback.answer()


@router.callback_query(F.data == "premium_confirm")
async def premium_confirm(callback: CallbackQuery):
    """Отправить заявку администратору: кто и для чего - для себя или для чата."""
    if not settings.admin_id:
        await callback.answer(t("premium.closed"), show_alert=True)
        return

    tg_user = callback.from_user
    now = time.monotonic()
    last = _last_request.get(tg_user.id)
    if last is not None and now - last < REQUEST_COOLDOWN_S:
        await callback.answer(t("premium.already"), show_alert=True)
        return
    # отметка - до первого await: иначе два быстрых нажатия проходят проверку оба
    # (находка security-reviewer 2026-09-30)
    _last_request[tg_user.id] = now

    chat = callback.message.chat
    who = escape(tg_user.full_name)
    if tg_user.username:
        who = f"@{escape(tg_user.username)} ({who})"
    target = t("premium.target_self", lang="ru")
    if chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        target = t("premium.target_chat", lang="ru", title=escape(chat.title or ""), id=chat.id)
    try:
        await callback.bot.send_message(
            settings.admin_id,
            t("premium.admin", lang="ru", who=who, id=tg_user.id, target=target),
        )
    except Exception as e:
        # администратор не открывал бота или заблокировал его - сообщение не уйдёт
        logger.error(f"Premium request not delivered to admin: {type(e).__name__}")
        _last_request.pop(tg_user.id, None)  # заявка не ушла - повтор разрешён
        await callback.answer(t("premium.send_failed"), show_alert=True)
        return

    logger.info(f"Premium request from user {tg_user.id} in {chat.type} chat")
    await callback.message.edit_text(t("premium.sent"))
    await callback.answer()


@router.callback_query(F.data == "cancel")
async def cancel_premium(callback: CallbackQuery):
    """Cancel premium selection."""
    await callback.answer(t("common.cancelled"))
    await callback.message.delete()
