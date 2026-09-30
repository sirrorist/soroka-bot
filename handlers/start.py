"""Start command handlers. Тексты - в locales/."""
from aiogram import Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import settings
from downloaders.common import max_send_mb
from utils.i18n import DEFAULT_LANG, get_lang, lang_for, t

router = Router()


def register_start_handlers(dp: Dispatcher) -> None:
    """Register start handlers."""
    dp.include_router(router)


@router.message(CommandStart())
async def cmd_start(message: Message):
    """Handle /start command."""
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text=t("start.button_how"), callback_data="download_menu")
    keyboard.button(text=t("start.button_stats"), callback_data="stats")
    keyboard.button(text=t("common.premium_button"), callback_data="premium")
    keyboard.button(text=t("start.button_help"), callback_data="help")
    keyboard.button(text=t("start.button_language"), callback_data="language")
    keyboard.adjust(2, 2, 1)

    text = t(
        "start.text",
        limits=t("common.limits_block", max_mb=max_send_mb()),
        free=settings.free_user_limit,
    )
    # по умолчанию бот русский; тем, у кого Telegram на другом языке, - как переключить
    if get_lang() == DEFAULT_LANG and lang_for(message.from_user and message.from_user.language_code) != DEFAULT_LANG:
        text += t("start.lang_hint")
    await message.answer(text, reply_markup=keyboard.as_markup())


@router.message(Command("help"))
@router.callback_query(F.data == "help")
async def cmd_help(message_or_query):
    """Handle /help command."""
    text = t("help.text", max_mb=max_send_mb(), p=settings.group_command_prefix)

    if hasattr(message_or_query, "message"):
        await message_or_query.message.edit_text(text)
        await message_or_query.answer()
    else:
        await message_or_query.answer(text)


@router.message(Command("about"))
async def cmd_about(message: Message):
    """Handle /about command."""
    await message.answer(t("about.text"))
