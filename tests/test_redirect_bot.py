"""redirect_bot.py: заглушка старого бота.

Модуль читает OLD_BOT_TOKEN и NEW_BOT_USERNAME из окружения при импорте, поэтому каждый
тест импортирует его заново (фикстура rb) с нужным окружением. Telegram не нужен:
отправка сообщений подменена, время - подставные часы.
"""
import datetime
import importlib
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from aiogram import Bot
from aiogram.types import Chat, Message, Update
from aiogram.types import User as TgUser


def load(monkeypatch, token="1:old", username="@Soroka_bot", photo=None):
    for name, value in (("OLD_BOT_TOKEN", token), ("NEW_BOT_USERNAME", username), ("MOVED_PHOTO", photo)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    monkeypatch.delitem(sys.modules, "redirect_bot", raising=False)
    return importlib.import_module("redirect_bot")


@pytest.fixture
def rb(monkeypatch, tmp_path):
    # каталога с moved.png в тестовом cwd может не быть - по умолчанию картинки нет
    monkeypatch.chdir(tmp_path)
    module = load(monkeypatch)
    yield module
    sys.modules.pop("redirect_bot", None)


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def monotonic(self):
        return self.now


# ---------- окружение при импорте ----------


def test_missing_token_fails_at_import(monkeypatch):
    with pytest.raises(KeyError, match="OLD_BOT_TOKEN"):
        load(monkeypatch, token=None)
    sys.modules.pop("redirect_bot", None)


def test_missing_username_fails_at_import(monkeypatch):
    with pytest.raises(KeyError, match="NEW_BOT_USERNAME"):
        load(monkeypatch, username=None)
    sys.modules.pop("redirect_bot", None)


def test_token_and_username_are_read_from_environment(monkeypatch):
    mod = load(monkeypatch, token="777:abc", username="Another_bot")
    try:
        assert mod.TOKEN == "777:abc"
        assert mod.NEW == "Another_bot"
        assert mod.LINK == "https://t.me/Another_bot"
    finally:
        sys.modules.pop("redirect_bot", None)


@pytest.mark.parametrize("given", ["@Soroka_bot", "Soroka_bot", "@@Soroka_bot"])
def test_at_sign_is_stripped_from_username(monkeypatch, given):
    mod = load(monkeypatch, username=given)
    try:
        assert mod.NEW == "Soroka_bot"
        assert mod.LINK == "https://t.me/Soroka_bot"
        for lang in ("ru", "en"):
            assert "@Soroka_bot" in mod.TEXTS[lang]["moved"]
            assert "@@" not in mod.TEXTS[lang]["moved"]
    finally:
        sys.modules.pop("redirect_bot", None)


def test_photo_path_comes_from_environment(monkeypatch):
    mod = load(monkeypatch, photo="/tmp/some/banner.png")
    try:
        assert str(mod.PHOTO) == "/tmp/some/banner.png"
    finally:
        sys.modules.pop("redirect_bot", None)


# ---------- тексты ----------


def test_texts_have_same_keys_in_both_languages_and_mention_new_bot(rb):
    assert set(rb.TEXTS["ru"]) == set(rb.TEXTS["en"]) == {"moved", "button", "about", "description"}
    for lang in ("ru", "en"):
        for key, text in rb.TEXTS[lang].items():
            assert text.strip(), (lang, key)
        assert "@Soroka_bot" in rb.TEXTS[lang]["moved"]
        assert "@Soroka_bot" in rb.TEXTS[lang]["about"]
        assert "@Soroka_bot" in rb.TEXTS[lang]["description"]


def test_texts_fit_telegram_limits(rb):
    for lang in ("ru", "en"):
        assert len(rb.TEXTS[lang]["moved"]) <= 1024  # подпись к картинке
        assert len(rb.TEXTS[lang]["about"]) <= 120  # short description бота
        assert len(rb.TEXTS[lang]["description"]) <= 512  # description бота


@pytest.mark.parametrize("lang", ["ru", "en"])
def test_keyboard_button_is_a_link_to_new_bot(rb, lang):
    kb = rb._keyboard(lang)
    button = kb.inline_keyboard[0][0]
    assert button.url == "https://t.me/Soroka_bot"
    assert button.text == rb.TEXTS[lang]["button"]


# ---------- _lang ----------


@pytest.mark.parametrize(
    "code, expected",
    [("ru", "ru"), ("RU", "ru"), ("ru-RU", "ru"), ("uk", "ru"), ("uk-UA", "ru"), ("be", "ru"),
     ("kk", "ru"), ("en", "en"), ("en-US", "en"), ("de", "en"), ("", "en"), (None, "en")],
)
def test_lang(rb, code, expected):
    assert rb._lang(code) == expected


# ---------- _due ----------


def test_due_first_call_passes_even_at_time_zero(rb, monkeypatch):
    monkeypatch.setattr(rb, "time", Clock(now=0.0))
    assert rb._due(1, "private") is True
    assert rb._due(-100, "group") is True


def test_due_private_once_a_minute(rb, monkeypatch):
    clock = Clock()
    monkeypatch.setattr(rb, "time", clock)
    assert rb._due(1, "private") is True
    assert rb._due(1, "private") is False
    clock.now += 59
    assert rb._due(1, "private") is False
    clock.now += 1
    assert rb._due(1, "private") is True


def test_due_group_once_an_hour(rb, monkeypatch):
    clock = Clock()
    monkeypatch.setattr(rb, "time", clock)
    assert rb._due(-100, "group") is True
    clock.now += 3599
    assert rb._due(-100, "group") is False
    clock.now += 1
    assert rb._due(-100, "group") is True


def test_due_refusal_does_not_extend_the_window(rb, monkeypatch):
    clock = Clock()
    monkeypatch.setattr(rb, "time", clock)
    rb._due(-100, "group")
    for _ in range(5):
        clock.now += 600
        rb._due(-100, "group")  # отказы каждые 10 минут
    clock.now += 600  # 3600 с от первого ответа
    assert rb._due(-100, "group") is True


def test_due_chats_are_independent(rb, monkeypatch):
    monkeypatch.setattr(rb, "time", Clock())
    assert rb._due(-100, "group") is True
    assert rb._due(-200, "group") is True
    assert rb._due(-100, "group") is False


# ---------- отправка ----------


@pytest.mark.asyncio
async def test_send_moved_without_photo_sends_text_with_button(rb):
    assert not rb.PHOTO.is_file()
    message = MagicMock(answer=AsyncMock(), answer_photo=AsyncMock())
    await rb._send_moved(message, "en")
    message.answer_photo.assert_not_awaited()
    message.answer.assert_awaited_once()
    assert message.answer.await_args.args == (rb.TEXTS["en"]["moved"],)
    assert message.answer.await_args.kwargs["disable_web_page_preview"] is True
    assert message.answer.await_args.kwargs["reply_markup"].inline_keyboard[0][0].url == rb.LINK


@pytest.mark.asyncio
async def test_send_moved_with_photo_uses_text_as_caption(monkeypatch, tmp_path):
    banner = tmp_path / "banner.png"
    banner.write_bytes(b"\x89PNG")
    mod = load(monkeypatch, photo=str(banner))
    try:
        message = MagicMock(answer=AsyncMock(), answer_photo=AsyncMock())
        await mod._send_moved(message, "ru")
        message.answer.assert_not_awaited()
        message.answer_photo.assert_awaited_once()
        assert message.answer_photo.await_args.kwargs["caption"] == mod.TEXTS["ru"]["moved"]
    finally:
        sys.modules.pop("redirect_bot", None)


# ---------- маршрутизация ----------


@pytest_asyncio.fixture
async def routed(rb, monkeypatch):
    """Настоящий Dispatcher заглушки; вместо отправки - запись (текст, язык)."""
    clock = Clock()
    monkeypatch.setattr(rb, "time", clock)
    sent = []

    async def fake_send(message, lang):
        sent.append((message.chat.id, lang))

    monkeypatch.setattr(rb, "_send_moved", fake_send)
    bot = Bot("1:x")

    async def feed(chat, text=None, lang_code=None, user_id=9):
        msg = Message(
            message_id=1,
            date=datetime.datetime(2026, 1, 1),
            text=text,
            chat=chat,
            from_user=TgUser(id=user_id, is_bot=False, first_name="t", language_code=lang_code),
        )
        await rb.dp.feed_update(bot, Update(update_id=1, message=msg))

    yield SimpleNamespace(feed=feed, sent=sent, clock=clock)
    await bot.session.close()


PRIVATE = Chat(id=1, type="private")
GROUP = Chat(id=-100, type="supergroup", title="g")


@pytest.mark.asyncio
async def test_private_message_gets_notice_in_users_language(routed):
    await routed.feed(PRIVATE, "привет", lang_code="uk")
    await routed.feed(Chat(id=2, type="private"), "hi", lang_code="en", user_id=10)
    assert routed.sent == [(1, "ru"), (2, "en")]


@pytest.mark.asyncio
async def test_private_repeat_within_a_minute_is_silent(routed):
    await routed.feed(PRIVATE, "раз")
    await routed.feed(PRIVATE, "два")
    assert routed.sent == [(1, "en")]  # без language_code - английский


@pytest.mark.asyncio
async def test_group_answers_commands_only(routed):
    await routed.feed(GROUP, "просто разговор")
    await routed.feed(GROUP, None)
    assert routed.sent == []
    await routed.feed(GROUP, "/start", lang_code="ru")
    assert routed.sent == [(-100, "ru")]


@pytest.mark.asyncio
async def test_group_command_repeat_within_an_hour_is_silent(routed):
    await routed.feed(GROUP, "/start")
    await routed.feed(GROUP, "/help")
    assert len(routed.sent) == 1


@pytest.mark.asyncio
async def test_private_answers_again_after_a_minute(routed):
    await routed.feed(PRIVATE, "раз")
    routed.clock.now += 60
    await routed.feed(PRIVATE, "два")
    assert len(routed.sent) == 2


@pytest.mark.asyncio
async def test_private_and_group_cooldowns_do_not_block_each_other(routed):
    await routed.feed(PRIVATE, "hi")
    await routed.feed(GROUP, "/start")
    assert [c for c, _ in routed.sent] == [1, -100]


@pytest.mark.asyncio
async def test_old_button_shows_alert_with_link_text(rb):
    callback = MagicMock(answer=AsyncMock())
    callback.from_user = SimpleNamespace(language_code="ru-RU")
    await rb.old_button(callback)
    callback.answer.assert_awaited_once_with("Бот переехал: @Soroka_bot", show_alert=True)

    other = MagicMock(answer=AsyncMock())
    other.from_user = SimpleNamespace(language_code=None)
    await rb.old_button(other)
    other.answer.assert_awaited_once_with("The bot has moved: @Soroka_bot", show_alert=True)


# ---------- профиль бота ----------


@pytest.mark.asyncio
async def test_setup_profile_sets_both_languages_and_clears_menu(rb):
    bot = MagicMock()
    bot.set_my_short_description = AsyncMock()
    bot.set_my_description = AsyncMock()
    bot.delete_my_commands = AsyncMock()

    await rb._setup_profile(bot)

    short = {c.kwargs["language_code"]: c.args[0] for c in bot.set_my_short_description.await_args_list}
    full = {c.kwargs["language_code"]: c.args[0] for c in bot.set_my_description.await_args_list}
    assert short == {None: rb.TEXTS["en"]["about"], "ru": rb.TEXTS["ru"]["about"]}
    assert full == {None: rb.TEXTS["en"]["description"], "ru": rb.TEXTS["ru"]["description"]}
    cleared = sorted(str(c.kwargs["language_code"]) for c in bot.delete_my_commands.await_args_list)
    assert cleared == ["None", "ru"]
