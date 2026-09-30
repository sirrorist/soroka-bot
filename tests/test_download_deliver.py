"""handlers/download.py: подпись к файлу, тексты лимита, deliver с fits/on_delivered.

Последний блок - сквозной: команда группы -> настоящий deliver -> настоящая SQLite,
загрузчик и Telegram подставные.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from config import settings
from database import Chat, Download, User
from downloaders.common import new_workdir
from handlers import download as dl
from handlers import group
from utils.i18n import set_lang

MB = 1024 * 1024

# ---------- _caption ----------


def test_caption_links_title_to_original():
    assert dl._caption("Смешное видео", "tiktok", "https://vt.tiktok.com/x/") == (
        '<a href="https://vt.tiktok.com/x/">Смешное видео</a>'
    )


def test_caption_escapes_title_html():
    got = dl._caption('<b>hi</b> & "q"', "youtube", "https://youtu.be/a")
    assert got == '<a href="https://youtu.be/a">&lt;b&gt;hi&lt;/b&gt; &amp; &quot;q&quot;</a>'


def test_caption_escapes_quote_and_ampersand_in_href():
    got = dl._caption("t", "youtube", 'https://youtu.be/a?x=1&y="2"')
    assert got == '<a href="https://youtu.be/a?x=1&amp;y=&quot;2&quot;">t</a>'
    # кавычка не выходит из атрибута
    assert 'y="2"' not in got


@pytest.mark.parametrize(
    "platform, name",
    [("tiktok", "TikTok"), ("youtube", "YouTube"), ("instagram", "Instagram"), ("twitter", "X"),
     ("mars", "mars")],
)
def test_caption_without_title_says_original_on_platform(platform, name):
    for title in (None, "", "   ", "\n"):
        assert dl._caption(title, platform, "https://e/x") == f'<a href="https://e/x">Оригинал на {name}</a>'


@pytest.mark.parametrize("placeholder", ["TikTok Video", "YouTube Content", "Instagram Content", "Twitter Content"])
def test_caption_placeholder_titles_from_downloaders_are_replaced(placeholder):
    assert dl._caption(placeholder, "tiktok", "https://e/x") == '<a href="https://e/x">Оригинал на TikTok</a>'
    # и с лишними пробелами по краям и внутри
    assert dl._caption(f"  {placeholder}\n", "tiktok", "https://e/x") == '<a href="https://e/x">Оригинал на TikTok</a>'


def test_caption_real_title_similar_to_placeholder_is_kept():
    assert dl._caption("TikTok Video 2", "tiktok", "https://e/x") == '<a href="https://e/x">TikTok Video 2</a>'


def test_caption_fallback_in_english():
    set_lang("en")
    assert dl._caption(None, "twitter", "https://e/x") == '<a href="https://e/x">Original on X</a>'


def test_caption_title_at_limit_is_kept_and_longer_is_cut():
    at_limit = "а" * 200
    assert dl._caption(at_limit, "tiktok", "u") == f'<a href="u">{at_limit}</a>'
    assert dl._caption("а" * 201, "tiktok", "u") == f'<a href="u">{"а" * 200}...</a>'
    assert dl._caption("а" * 5000, "tiktok", "u") == f'<a href="u">{"а" * 200}...</a>'


def test_caption_cut_does_not_leave_space_before_dots():
    title = "а" * 199 + " " + "б" * 50
    assert dl._caption(title, "tiktok", "u") == f'<a href="u">{"а" * 199}...</a>'


def test_caption_collapses_whitespace_in_title():
    assert dl._caption("раз\n  два\t три", "tiktok", "u") == '<a href="u">раз два три</a>'


# ---------- limit_text ----------


def test_limit_text_free(monkeypatch):
    monkeypatch.setattr(settings, "free_user_limit", 7)
    monkeypatch.setattr(settings, "premium_user_limit", 1000)
    assert dl.limit_text(False) == (
        "<b>На сегодня всё: 7 из 7</b>\nОбновится в 3:00 по Москве.\n\n"
        "⭐ С Premium - <b>1000 скачиваний в день</b>."
    )


def test_limit_text_premium_has_no_upsell(monkeypatch):
    monkeypatch.setattr(settings, "free_user_limit", 7)
    monkeypatch.setattr(settings, "premium_user_limit", 1000)
    text = dl.limit_text(True)
    assert text == "<b>На сегодня всё: 1000 из 1000</b>\nОбновится в 3:00 по Москве."
    assert "С Premium" not in text and "7" not in text


# ---------- deliver ----------


class Env:
    def __init__(self, monkeypatch):
        self.calls: list[str] = []
        self.processing = MagicMock()
        self.processing.edit_text = AsyncMock()
        self.processing.delete = AsyncMock(side_effect=lambda: self.calls.append("delete"))
        self.downloader = AsyncMock()
        self.reserve = AsyncMock(return_value=True)
        self.release = AsyncMock(side_effect=lambda: self.calls.append("release"))
        self.fits = AsyncMock(return_value=None)
        self.on_delivered = AsyncMock(side_effect=lambda size: self.calls.append(f"delivered:{size}"))
        for platform in ("tiktok", "youtube", "instagram", "twitter"):
            monkeypatch.setitem(dl.DOWNLOADERS, platform, self.downloader)
        monkeypatch.setattr(dl, "_active_users", set())
        monkeypatch.setattr(dl, "_download_slots", asyncio.Semaphore(3))
        # запись в историю - не предмет этих тестов: пользователя в базе нет
        monkeypatch.setattr(dl, "get_db", self._empty_db)

        self.message = MagicMock()
        self.message.answer = AsyncMock(return_value=self.processing)
        self.message.answer_video = AsyncMock(side_effect=lambda *a, **k: self.calls.append("video"))

    @staticmethod
    async def _empty_db():
        session = MagicMock()
        session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None)))
        session.commit = AsyncMock()
        yield session

    def file(self, size=2048):
        workdir = new_workdir("youtube")
        f = workdir / "youtube_abc.mp4"
        f.write_bytes(b"x" * 16)
        return {"success": True, "file_path": str(f), "content_type": "video", "file_size": size,
                "title": "Ролик"}, workdir

    async def run(self, **extra):
        await dl.deliver(
            self.message,
            user_id=7,
            premium=False,
            platform="youtube",
            url="https://youtu.be/abc",
            reserve=self.reserve,
            release=self.release,
            limit_text="LIMIT",
            **extra,
        )


@pytest.fixture
def env(monkeypatch, workroot):
    return Env(monkeypatch)


@pytest.mark.asyncio
async def test_fits_refusal_returns_slot_and_sends_nothing(env):
    result, workdir = env.file(size=60 * MB)
    env.downloader.return_value = result
    env.fits.return_value = "Файл не влезает"

    await env.run(fits=env.fits, on_delivered=env.on_delivered)

    env.fits.assert_awaited_once_with(60 * MB)
    env.processing.edit_text.assert_awaited_once_with("Файл не влезает\n<i>Попытка не списана.</i>")
    env.message.answer_video.assert_not_awaited()
    env.on_delivered.assert_not_awaited()
    env.release.assert_awaited_once()
    assert not workdir.exists()
    assert dl._active_users == set()


@pytest.mark.asyncio
async def test_fits_none_delivers_then_reports_size_after_sending(env):
    result, workdir = env.file(size=3 * MB)
    env.downloader.return_value = result

    await env.run(fits=env.fits, on_delivered=env.on_delivered)

    env.fits.assert_awaited_once_with(3 * MB)
    env.on_delivered.assert_awaited_once_with(3 * MB)
    env.release.assert_not_awaited()
    # учёт - строго после отправки файла, а не до неё
    assert env.calls == ["video", "delete", f"delivered:{3 * MB}"]
    assert not workdir.exists()


@pytest.mark.asyncio
async def test_without_fits_and_on_delivered_deliver_still_works(env):
    result, _ = env.file()
    env.downloader.return_value = result
    await env.run()
    env.message.answer_video.assert_awaited_once()
    env.release.assert_not_awaited()


@pytest.mark.asyncio
async def test_fits_is_not_asked_for_file_over_global_limit(env, monkeypatch):
    monkeypatch.setattr(settings, "max_file_size_mb", 1)
    result, _ = env.file(size=5 * MB)
    env.downloader.return_value = result
    await env.run(fits=env.fits, on_delivered=env.on_delivered)
    env.fits.assert_not_awaited()
    env.on_delivered.assert_not_awaited()
    env.release.assert_awaited_once()


@pytest.mark.asyncio
async def test_on_delivered_not_called_when_download_failed(env):
    env.downloader.return_value = {"success": False, "error": "boom"}
    await env.run(fits=env.fits, on_delivered=env.on_delivered)
    env.on_delivered.assert_not_awaited()
    env.fits.assert_not_awaited()
    env.release.assert_awaited_once()


@pytest.mark.asyncio
async def test_on_delivered_not_called_when_telegram_send_fails(env):
    result, workdir = env.file()
    env.downloader.return_value = result
    env.message.answer_video = AsyncMock(side_effect=RuntimeError("telegram down"))

    await env.run(fits=env.fits, on_delivered=env.on_delivered)

    env.on_delivered.assert_not_awaited()
    env.release.assert_awaited_once()
    env.processing.edit_text.assert_awaited_once_with("Что-то пошло не так. Попробуйте ещё раз чуть позже.\n<i>Попытка не списана.</i>")
    assert not workdir.exists()


@pytest.mark.asyncio
async def test_fits_error_returns_slot(env):
    result, _ = env.file()
    env.downloader.return_value = result
    env.fits.side_effect = RuntimeError("db down")
    await env.run(fits=env.fits, on_delivered=env.on_delivered)
    env.message.answer_video.assert_not_awaited()
    env.release.assert_awaited_once()
    assert dl._active_users == set()


@pytest.mark.asyncio
async def test_on_delivered_error_after_sending_does_not_return_slot(env):
    result, _ = env.file()
    env.downloader.return_value = result
    env.on_delivered.side_effect = RuntimeError("db down")

    await env.run(fits=env.fits, on_delivered=env.on_delivered)  # исключение наружу не летит

    env.message.answer_video.assert_awaited_once()
    env.release.assert_not_awaited()  # файл уже у людей - слот потрачен
    assert dl._active_users == set()


@pytest.mark.asyncio
async def test_limit_refusal_uses_given_text_and_upsell_button_only_for_free(env):
    env.reserve.return_value = False
    await env.run()
    env.message.answer.assert_awaited_once()
    assert env.message.answer.await_args.args == ("LIMIT",)
    assert env.message.answer.await_args.kwargs["reply_markup"] is not None
    env.release.assert_not_awaited()
    env.downloader.assert_not_awaited()


# ---------- сквозной: /sr_dl -> deliver -> база ----------


class Flow:
    """Команда группы с настоящим deliver, SQLite и подставными загрузчиком и Telegram."""

    def __init__(self, db, monkeypatch, workroot):
        self.db = db
        self.processing = MagicMock(edit_text=AsyncMock(), delete=AsyncMock())
        self.downloader = AsyncMock()
        for platform in ("tiktok", "youtube", "instagram", "twitter"):
            monkeypatch.setitem(dl.DOWNLOADERS, platform, self.downloader)
        monkeypatch.setattr(dl, "_active_users", set())
        monkeypatch.setattr(dl, "_download_slots", asyncio.Semaphore(3))
        monkeypatch.setattr(settings, "group_free_limit", 2)
        monkeypatch.setattr(settings, "group_free_daily_mb", 1)

    def message(self):
        m = MagicMock()
        m.chat = SimpleNamespace(id=-100, title="Чат")
        m.reply_to_message = None
        m.answer = AsyncMock(return_value=self.processing)
        m.answer_video = AsyncMock()
        return m

    def result(self, size):
        workdir = new_workdir("youtube")
        f = workdir / "youtube_abc.mp4"
        f.write_bytes(b"x")
        return {"success": True, "file_path": str(f), "content_type": "video", "file_size": size, "title": "T"}

    async def command(self, size):
        self.downloader.return_value = self.result(size)
        m = self.message()
        await group.group_download(
            m, SimpleNamespace(args="https://youtu.be/abc", command="sr_dl"), user=SimpleNamespace(id=7)
        )
        return m

    async def chat(self):
        async with self.db() as s:
            return await s.get(Chat, -100)


@pytest.fixture
def flow(db, monkeypatch, workroot):
    return Flow(db, monkeypatch, workroot)


@pytest.mark.asyncio
async def test_group_flow_delivery_is_counted_against_chat_and_history(flow, db):
    async with db() as s:
        s.add(User(id=7, first_name="Аня", total_downloads=0, downloads_today=0))
        await s.commit()

    m = await flow.command(size=MB // 2)

    m.answer_video.assert_awaited_once()
    chat = await flow.chat()
    assert (chat.downloads_today, chat.bytes_today, chat.total_downloads) == (1, MB // 2, 1)
    async with db() as s:
        history = (await s.execute(Download.__table__.select())).all()
        user = await s.get(User, 7)
    assert len(history) == 1 and history[0].file_size == MB // 2
    # личный счётчик участника группа не трогает: лимит тратит чат
    assert user.downloads_today == 0


@pytest.mark.asyncio
async def test_group_flow_file_over_remainder_is_refused_and_slot_returned(flow):
    m = await flow.command(size=2 * MB)

    m.answer_video.assert_not_awaited()
    flow.processing.edit_text.assert_awaited_once_with(
        "Файл 2,0 МБ не влезает в дневной объём чата - осталось 1,0 МБ.\n<i>Попытка не списана.</i>"
    )
    chat = await flow.chat()
    assert (chat.downloads_today, chat.bytes_today, chat.total_downloads) == (0, 0, 0)


@pytest.mark.asyncio
async def test_group_flow_count_limit_stops_third_download(flow):
    await flow.command(size=1000)
    await flow.command(size=1000)
    third = await flow.command(size=1000)

    third.answer_video.assert_not_awaited()
    text = third.answer.await_args.args[0]
    assert text.startswith("<b>Лимит чата на сегодня исчерпан</b> - 2 скачивания или 1,0 МБ.")
    assert "/sr_premium" in text
    chat = await flow.chat()
    assert (chat.downloads_today, chat.total_downloads) == (2, 2)


@pytest.mark.asyncio
async def test_group_flow_volume_limit_stops_next_download_even_below_count(flow):
    first = await flow.command(size=MB)  # ровно дневной объём: влезает
    first.answer_video.assert_awaited_once()
    second = await flow.command(size=1)
    second.answer_video.assert_not_awaited()
    assert "Лимит чата на сегодня исчерпан" in second.answer.await_args.args[0]
