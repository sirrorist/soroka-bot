"""handlers/premium.py: заявка на Premium администратору.

Telegram не нужен: callback, бот и часы - подставные. Главное: заявка не уходит без
ADMIN_ID, не чаще раза в час, имена экранируются, при сбое отправки человек может
повторить.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from loguru import logger

from config import settings
from handlers import premium
from utils.i18n import set_lang

ADMIN = 424242


class Clock:
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(premium, "time", c)
    monkeypatch.setattr(premium, "_last_request", {})
    return c


@pytest.fixture(autouse=True)
def admin(monkeypatch):
    monkeypatch.setattr(settings, "admin_id", ADMIN)


def make_callback(user_id=5, username="anya", full_name="Аня К", chat_type="private", title=None, chat_id=5):
    cb = MagicMock()
    cb.from_user = SimpleNamespace(id=user_id, username=username, full_name=full_name)
    cb.message.chat = SimpleNamespace(type=chat_type, title=title, id=chat_id)
    cb.message.edit_text = AsyncMock()
    cb.answer = AsyncMock()
    cb.bot.send_message = AsyncMock()
    return cb


def sent_text(cb):
    return cb.bot.send_message.await_args.args[1]


ADMIN_PRIVATE = (
    '⭐ <b>Заявка на Premium</b>\n\n<b>От:</b> @anya (Аня К)\n<b>ID:</b> <code>5</code>\n'
    '<b>Для:</b> лично для себя\n\n<a href="tg://user?id=5">Открыть профиль</a>'
)


@pytest.mark.asyncio
@pytest.mark.parametrize("admin_id", [None, 0])
async def test_no_admin_id_refuses_and_sends_nothing(clock, monkeypatch, admin_id):
    monkeypatch.setattr(settings, "admin_id", admin_id)
    cb = make_callback()
    await premium.premium_confirm(cb)
    cb.bot.send_message.assert_not_awaited()
    cb.answer.assert_awaited_once_with("Заявки пока не принимаются. Попробуйте позже.", show_alert=True)
    cb.message.edit_text.assert_not_awaited()
    assert premium._last_request == {}


@pytest.mark.asyncio
async def test_request_goes_to_admin_with_exact_text_and_user_gets_confirmation(clock):
    cb = make_callback()
    await premium.premium_confirm(cb)

    cb.bot.send_message.assert_awaited_once()
    assert cb.bot.send_message.await_args.args[0] == ADMIN
    assert sent_text(cb) == ADMIN_PRIVATE
    cb.message.edit_text.assert_awaited_once_with(
        "✅ <b>Заявка отправлена.</b> Администратор скоро с Вами свяжется."
    )
    cb.answer.assert_awaited_once_with()
    assert premium._last_request == {5: 1000.0}


@pytest.mark.asyncio
async def test_user_without_username_is_shown_by_name_only(clock):
    cb = make_callback(username=None)
    await premium.premium_confirm(cb)
    assert "<b>От:</b> Аня К\n" in sent_text(cb)
    assert "@" not in sent_text(cb)


@pytest.mark.asyncio
async def test_repeat_within_hour_is_refused_and_not_sent_again(clock):
    await premium.premium_confirm(make_callback())

    clock.now += 3599
    second = make_callback()
    await premium.premium_confirm(second)

    second.bot.send_message.assert_not_awaited()
    second.answer.assert_awaited_once_with(
        "Заявка уже отправлена - администратор скоро напишет.", show_alert=True
    )
    second.message.edit_text.assert_not_awaited()
    # отказ не продлевает окно: отсчёт идёт от отправленной заявки
    assert premium._last_request == {5: 1000.0}


@pytest.mark.asyncio
async def test_repeat_after_an_hour_is_allowed(clock):
    await premium.premium_confirm(make_callback())
    clock.now += 3600
    again = make_callback()
    await premium.premium_confirm(again)
    again.bot.send_message.assert_awaited_once()
    assert premium._last_request == {5: 4600.0}


@pytest.mark.asyncio
async def test_cooldown_is_per_person(clock):
    await premium.premium_confirm(make_callback(user_id=5))
    other = make_callback(user_id=6, username="boris", full_name="Борис")
    await premium.premium_confirm(other)
    other.bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_name_and_username_are_escaped_for_admin_html(clock):
    cb = make_callback(username="a<b>&", full_name='<b>x</b> & "Co"')
    await premium.premium_confirm(cb)
    text = sent_text(cb)
    assert "@a&lt;b&gt;&amp; (&lt;b&gt;x&lt;/b&gt; &amp; &quot;Co&quot;)" in text
    assert "<b>x</b>" not in text


@pytest.mark.asyncio
async def test_group_request_names_the_chat_and_escapes_title(clock):
    cb = make_callback(chat_type="supergroup", title='<i>"Чат"</i>', chat_id=-1001234)
    await premium.premium_confirm(cb)
    text = sent_text(cb)
    assert '<b>Для:</b> для чата "&lt;i&gt;&quot;Чат&quot;&lt;/i&gt;" (<code>-1001234</code>)' in text
    assert "лично для себя" not in text


@pytest.mark.asyncio
async def test_plain_group_and_missing_title_are_handled(clock):
    cb = make_callback(chat_type="group", title=None, chat_id=-77)
    await premium.premium_confirm(cb)
    assert '<b>Для:</b> для чата "" (<code>-77</code>)' in sent_text(cb)


@pytest.mark.asyncio
async def test_channel_or_other_chat_type_counts_as_personal(clock):
    cb = make_callback(chat_type="channel")
    await premium.premium_confirm(cb)
    assert "лично для себя" in sent_text(cb)


@pytest.mark.asyncio
async def test_admin_message_stays_russian_when_user_chose_english(clock):
    set_lang("en")
    cb = make_callback()
    await premium.premium_confirm(cb)
    assert sent_text(cb) == ADMIN_PRIVATE
    # а человеку ответ - на его языке
    assert cb.message.edit_text.await_args.args[0] != "✅ <b>Заявка отправлена.</b> Администратор скоро с Вами свяжется."


@pytest.mark.asyncio
async def test_two_fast_presses_send_one_request(clock):
    """Отметка о заявке ставится до отправки: пока первая едет, вторая уже отказана."""
    first, second = make_callback(), make_callback()

    async def slow_send(*args, **kwargs):
        await asyncio.sleep(0.05)

    first.bot.send_message.side_effect = slow_send
    second.bot.send_message.side_effect = slow_send

    await asyncio.gather(premium.premium_confirm(first), premium.premium_confirm(second))

    sent = first.bot.send_message.await_count + second.bot.send_message.await_count
    assert sent == 1
    refused = [c for c in (first, second) if c.bot.send_message.await_count == 0][0]
    refused.answer.assert_awaited_once_with(
        "Заявка уже отправлена - администратор скоро напишет.", show_alert=True
    )


@pytest.mark.asyncio
async def test_send_failure_refuses_and_allows_retry(clock):
    cb = make_callback()
    cb.bot.send_message.side_effect = RuntimeError("Forbidden: bot can't initiate conversation")
    await premium.premium_confirm(cb)

    cb.answer.assert_awaited_once_with("Не получилось отправить заявку. Попробуйте позже.", show_alert=True)
    cb.message.edit_text.assert_not_awaited()  # "заявка отправлена" не показывается зря
    assert premium._last_request == {}

    retry = make_callback()
    await premium.premium_confirm(retry)  # окно не занято - повтор проходит сразу
    retry.bot.send_message.assert_awaited_once()
    retry.message.edit_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_send_failure_logs_only_exception_type(clock):
    lines = []
    sink = logger.add(lambda m: lines.append(m.record["message"]), level="INFO")
    try:
        cb = make_callback()
        cb.bot.send_message.side_effect = RuntimeError("secret chat_id=424242 token=abc")
        await premium.premium_confirm(cb)
    finally:
        logger.remove(sink)
    joined = "\n".join(lines)
    assert "RuntimeError" in joined
    assert "secret" not in joined and "424242" not in joined


# ---------- premium_request / show_premium ----------


@pytest.mark.asyncio
async def test_request_asks_consent_and_warns_about_missing_username():
    with_name = make_callback(username="anya")
    await premium.premium_request(with_name)
    text = with_name.message.edit_text.await_args.args[0]
    assert text.startswith("⚠️ <b>Передать Ваш ник администратору?</b>")
    assert "не задан ник" not in text

    without = make_callback(username=None)
    await premium.premium_request(without)
    assert "У Вас не задан ник в Telegram" in without.message.edit_text.await_args.args[0]

    buttons = with_name.message.edit_text.await_args.kwargs["reply_markup"].inline_keyboard[0]
    assert [b.callback_data for b in buttons] == ["premium_confirm", "cancel"]


@pytest.mark.asyncio
async def test_show_premium_offer_for_free_user_has_request_button(monkeypatch):
    monkeypatch.setattr(settings, "premium_user_limit", 1000)
    monkeypatch.setattr(settings, "free_user_limit", 7)
    monkeypatch.setattr(settings, "premium_price_month", 99)
    monkeypatch.setattr(settings, "premium_price_year", 599)
    msg = SimpleNamespace(answer=AsyncMock())
    await premium.show_premium(msg, user=SimpleNamespace(is_premium=False))

    text = msg.answer.await_args.args[0]
    assert "<b>1000 скачиваний в день</b> вместо 7." in text
    assert "Месяц - <b>99 ₽</b>" in text and "Год - <b>599 ₽</b>" in text
    kbd = msg.answer.await_args.kwargs["reply_markup"]
    assert kbd.inline_keyboard[0][0].callback_data == "premium_request"


@pytest.mark.asyncio
async def test_show_premium_for_premium_user_has_no_offer_and_no_button(monkeypatch):
    monkeypatch.setattr(settings, "premium_user_limit", 1000)
    msg = SimpleNamespace(answer=AsyncMock())
    await premium.show_premium(msg, user=SimpleNamespace(is_premium=True))
    text = msg.answer.await_args.args[0]
    assert text.startswith("⭐ <b>У Вас Premium</b>")
    assert msg.answer.await_args.kwargs["reply_markup"] is None


@pytest.mark.asyncio
async def test_show_premium_without_user_is_treated_as_free():
    msg = SimpleNamespace(answer=AsyncMock())
    await premium.show_premium(msg, user=None)
    assert msg.answer.await_args.kwargs["reply_markup"] is not None
