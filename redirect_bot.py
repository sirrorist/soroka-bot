"""Старый бот после переезда: на любое сообщение отвечает, что бот переехал.

Один файл без базы и без остального кода - слово владельца 2026-09-30. Юзернейм
бота в Telegram не меняется, поэтому старый @tiktubeDownloadBot остаётся жить
заглушкой, а Soroka работает под новым токеном.

Запуск: сервис redirect в docker-compose.yml (тот же образ) или
    OLD_BOT_TOKEN=... NEW_BOT_USERNAME=... python redirect_bot.py
Переменные:
    OLD_BOT_TOKEN     - токен старого бота (обязательно)
    NEW_BOT_USERNAME  - юзернейм нового бота без @ (обязательно)
    MOVED_PHOTO       - картинка к сообщению (баннер, скриншот); нет файла - только текст

Режим - polling: webhook старого бота снимается при старте, traefik заглушке не нужен.
"""
import asyncio
import os
import time
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType, ParseMode
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from loguru import logger

TOKEN = os.environ["OLD_BOT_TOKEN"]
NEW = os.environ["NEW_BOT_USERNAME"].lstrip("@")
PHOTO = Path(os.environ.get("MOVED_PHOTO", "moved.png"))
LINK = f"https://t.me/{NEW}"

# Тексты - здесь, а не в locales/: заглушка живёт одним файлом
TEXTS = {
    "ru": {
        "moved": (
            "🐦 <b>Бот переехал!</b>\n\n"
            f"Теперь он называется <b>Soroka</b> и живёт здесь: @{NEW}\n\n"
            "Всё как раньше: пришлите ссылку на ролик из TikTok, YouTube, Instagram или X - "
            "и получите файл. Ваша статистика и Premium переехали вместе с ботом.\n\n"
            "Этот бот больше ничего не скачивает."
        ),
        "button": "Открыть Soroka",
        "about": f"Бот переехал: @{NEW}",
        "description": (
            f"Этот бот переехал и больше не скачивает. Новый адрес - @{NEW}: "
            "всё то же самое, статистика и Premium сохранились."
        ),
    },
    "en": {
        "moved": (
            "🐦 <b>The bot has moved!</b>\n\n"
            f"It's now called <b>Soroka</b> and lives here: @{NEW}\n\n"
            "Everything works as before: send a link to a video from TikTok, YouTube, "
            "Instagram or X and get the file. Your stats and Premium moved too.\n\n"
            "This bot no longer downloads anything."
        ),
        "button": "Open Soroka",
        "about": f"The bot has moved: @{NEW}",
        "description": (
            f"This bot has moved and no longer downloads. The new address is @{NEW}: "
            "same features, your stats and Premium are kept."
        ),
    },
}
RU_LANGS = {"ru", "uk", "be", "kk"}
# в группе - не чаще раза в час на чат, в личке - раза в минуту на человека
COOLDOWN_S = {"group": 3600, "private": 60}
_last: dict[int, float] = {}

dp = Dispatcher()


def _lang(code: str | None) -> str:
    return "ru" if (code or "").split("-")[0].lower() in RU_LANGS else "en"


def _keyboard(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=TEXTS[lang]["button"], url=LINK)]]
    )


def _due(chat_id: int, kind: str) -> bool:
    """Не отвечать чаще COOLDOWN_S: иначе в активной группе бот засорит чат."""
    now = time.monotonic()
    if now - _last.get(chat_id, -COOLDOWN_S[kind]) < COOLDOWN_S[kind]:
        return False
    _last[chat_id] = now
    return True


async def _send_moved(message: Message, lang: str) -> None:
    text, keyboard = TEXTS[lang]["moved"], _keyboard(lang)
    if PHOTO.is_file():
        await message.answer_photo(FSInputFile(PHOTO), caption=text, reply_markup=keyboard)
    else:
        await message.answer(text, reply_markup=keyboard, disable_web_page_preview=True)


@dp.message(F.chat.type == ChatType.PRIVATE)
async def private(message: Message) -> None:
    if _due(message.chat.id, "private"):
        await _send_moved(message, _lang(message.from_user and message.from_user.language_code))


@dp.message(F.text.startswith("/"))
async def group_command(message: Message) -> None:
    """В группе отвечает только на команды - как и раньше, раз в час на чат."""
    if _due(message.chat.id, "group"):
        await _send_moved(message, _lang(message.from_user and message.from_user.language_code))


@dp.callback_query()
async def old_button(callback: CallbackQuery) -> None:
    """Кнопки под старыми сообщениями: всплывающее уведомление со ссылкой."""
    lang = _lang(callback.from_user.language_code)
    await callback.answer(TEXTS[lang]["about"], show_alert=True)


async def _setup_profile(bot: Bot) -> None:
    """Профиль старого бота тоже говорит о переезде; меню команд - пустое."""
    for lang, code in (("en", None), ("ru", "ru")):
        await bot.set_my_short_description(TEXTS[lang]["about"], language_code=code)
        await bot.set_my_description(TEXTS[lang]["description"], language_code=code)
        await bot.delete_my_commands(language_code=code)


async def main() -> None:
    bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    # старый бот работал через webhook - без его снятия polling не получит апдейтов
    await bot.delete_webhook(drop_pending_updates=False)
    try:
        await _setup_profile(bot)
    except Exception as e:
        logger.warning(f"Profile not updated: {type(e).__name__}")
    logger.info(f"Redirect bot started, pointing to @{NEW}")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
