"""/language - выбор языка бота. По умолчанию русский, выбор запоминается."""

from aiogram import Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

from utils.i18n import LANGS, set_lang, t
from utils.lang_store import save_user_lang
from utils.menu import set_chat_menu

router = Router()


def register_language_handlers(dp: Dispatcher) -> None:
    """Register language handlers."""
    dp.include_router(router)


def _keyboard():
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text=t("language.button_ru"), callback_data="lang:ru")
    keyboard.button(text=t("language.button_en"), callback_data="lang:en")
    keyboard.adjust(2)
    return keyboard.as_markup()


@router.message(Command("language"))
@router.callback_query(F.data == "language")
async def cmd_language(message_or_query):
    """Показать выбор языка."""
    if isinstance(message_or_query, CallbackQuery):
        await message_or_query.message.edit_text(
            t("language.choose"), reply_markup=_keyboard()
        )
        await message_or_query.answer()
    else:
        await message_or_query.answer(t("language.choose"), reply_markup=_keyboard())


@router.callback_query(F.data.startswith("lang:"))
async def choose_language(callback: CallbackQuery):
    """Сохранить язык, сменить меню команд этого чата и ответить уже на новом языке."""
    lang = callback.data.split(":", 1)[1]
    if lang not in LANGS:
        await callback.answer()
        return
    await save_user_lang(callback.from_user.id, lang)
    set_lang(lang)
    if callback.message.chat.type == "private":
        await set_chat_menu(callback.bot, callback.message.chat.id, lang)
    await callback.message.edit_text(t("language.saved"))
    await callback.answer()
