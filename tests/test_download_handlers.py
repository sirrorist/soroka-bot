"""Распознавание ссылок и handlers/download.py: handle_url, лимит, очистка, экранирование.

Сети и настоящей базы нет: загрузчики, session_scope и сообщения - моки, каталог - в tmp_path.
"""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.dialects import postgresql

from config import settings
from database import Download
from downloaders import detect_platform
from downloaders.common import extract_url, new_workdir
from handlers import download as dl

# ---------- extract_url ----------


@pytest.mark.parametrize(
    "text, platform, url",
    [
        (
            "https://vt.tiktok.com/ZSaD796vL/",
            "tiktok",
            "https://vt.tiktok.com/ZSaD796vL/",
        ),
        (
            "https://vm.tiktok.com/ZSaD796vL/",
            "tiktok",
            "https://vm.tiktok.com/ZSaD796vL/",
        ),
        ("https://youtu.be/dQw4w9WgXcQ", "youtube", "https://youtu.be/dQw4w9WgXcQ"),
        (
            "https://www.tiktok.com/@aliushkaa1/video/7593764380861385991",
            "tiktok",
            "https://www.tiktok.com/@aliushkaa1/video/7593764380861385991",
        ),
        (
            "https://m.youtube.com/watch?v=abc",
            "youtube",
            "https://m.youtube.com/watch?v=abc",
        ),
        ("http://tiktok.com/@u/video/1", "tiktok", "http://tiktok.com/@u/video/1"),
        ("https://x.com/user/status/1", "twitter", "https://x.com/user/status/1"),
        (
            "https://www.instagram.com/p/ABC/",
            "instagram",
            "https://www.instagram.com/p/ABC/",
        ),
        # ссылка внутри текста, окружающие слова и пунктуация не попадают в URL
        ("смотри https://youtu.be/abc, класс", "youtube", "https://youtu.be/abc"),
        ("(https://youtu.be/abc)", "youtube", "https://youtu.be/abc"),
        ("вот: https://vt.tiktok.com/ZS1/.", "tiktok", "https://vt.tiktok.com/ZS1/"),
        ("<https://youtu.be/abc>", "youtube", "https://youtu.be/abc"),
        ("https://youtu.be/abc!", "youtube", "https://youtu.be/abc"),
        # схема необязательна
        ("tiktok.com/@u/video/1", "tiktok", "https://tiktok.com/@u/video/1"),
        ("youtu.be/abc", "youtube", "https://youtu.be/abc"),
        (
            "www.youtube.com/watch?v=abc",
            "youtube",
            "https://www.youtube.com/watch?v=abc",
        ),
        # регистр схемы и хоста
        ("HTTPS://WWW.TikTok.com/x", "tiktok", "https://WWW.TikTok.com/x"),
        # первая подходящая ссылка, чужие до неё пропускаются
        (
            "http://evil.example/ https://youtu.be/ok",
            "youtube",
            "https://youtu.be/ok",
        ),
    ],
)
def test_extract_url_accepts(text, platform, url):
    assert extract_url(text) == (platform, url)


@pytest.mark.parametrize(
    "text",
    [
        "http://evil.example/?a=tiktok.com",
        "https://linux.com/x",
        "http://169.254.169.254/#tiktok.com",
        "https://tiktok.com@evil.example/",
        "https://user:pass@tiktok.com/x",
        "https://tiktok.com:8080/x",
        "http://youtube.com:80/x",
        "file:///etc/passwd",
        "ytsearch5:cats",
        "https://nottiktok.com/x",
        "https://tiktok.com.evil.example/",
        "https://evil.example/tiktok.com",
        "ftp://tiktok.com/x",
        "javascript://tiktok.com/%0aalert(1)",
        "https://evil.example\\@tiktok.com/",
        "https://evil.example#@tiktok.com/",
        "http://127.0.0.1/",
        "http://[::1]/",
        "https://faketiktok.com",
        "https://-tiktok.com/x",
        "привет, как дела",
        "",
        "   ",
        "tiktok",
    ],
)
def test_extract_url_rejects(text):
    assert extract_url(text) is None


def test_extract_url_takes_suffix_only_after_dot():
    assert extract_url("https://a.b.tiktok.com/x") == (
        "tiktok",
        "https://a.b.tiktok.com/x",
    )
    assert extract_url("https://xtiktok.com/x") is None


def test_detect_platform_uses_host_not_substring():
    assert detect_platform("https://youtu.be/dQw4w9WgXcQ") == "youtube"
    assert detect_platform("http://evil.example/?a=tiktok.com") is None


# ---------- _reserve_download / _release_download ----------


def _session_factory(session):
    @asynccontextmanager
    async def session_scope():
        yield session

    return session_scope


def _sql(stmt):
    return str(
        stmt.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("rowcount, expected", [(1, True), (0, False)])
async def test_reserve_download_is_single_conditional_update(
    monkeypatch, rowcount, expected
):
    session = MagicMock()
    session.execute = AsyncMock(return_value=SimpleNamespace(rowcount=rowcount))
    session.commit = AsyncMock()
    monkeypatch.setattr(dl, "session_scope", _session_factory(session))

    assert await dl._reserve_download(5, 7) is expected

    session.execute.assert_awaited_once()
    sql = _sql(session.execute.await_args.args[0])
    assert sql.startswith("UPDATE users SET downloads_today=")
    assert "coalesce(users.downloads_today, 0) + 1" in sql
    # проверка лимита и увеличение - в одном UPDATE, иначе два запроса пройдут вместе
    assert "WHERE users.id = 5 AND coalesce(users.downloads_today, 0) < 7" in sql
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_release_download_decrements_but_never_below_zero(monkeypatch):
    session = MagicMock()
    session.execute = AsyncMock()
    session.commit = AsyncMock()
    monkeypatch.setattr(dl, "session_scope", _session_factory(session))

    await dl._release_download(5)

    session.execute.assert_awaited_once()
    sql = _sql(session.execute.await_args.args[0])
    assert "SET downloads_today=(users.downloads_today - 1)" in sql
    assert "WHERE users.id = 5 AND users.downloads_today > 0" in sql
    session.commit.assert_awaited_once()


# ---------- handle_url ----------

USER_ID = 123
URL = "https://youtu.be/abc"


class Harness:
    """Всё окружение handle_url: сообщение, лимит, загрузчики, база."""

    def __init__(self, monkeypatch, workroot):
        self.workroot = workroot
        self.processing = MagicMock()
        self.processing.edit_text = AsyncMock()
        self.processing.delete = AsyncMock()
        self.reserve = AsyncMock(return_value=True)
        self.release = AsyncMock()
        self.downloader = AsyncMock()
        self.added = []
        self.fresh_user = SimpleNamespace(total_downloads=10)

        session = MagicMock()
        session.execute = AsyncMock(
            return_value=MagicMock(
                scalar_one_or_none=MagicMock(return_value=self.fresh_user)
            )
        )
        session.add = MagicMock(side_effect=self.added.append)
        session.commit = AsyncMock()
        self.session = session

        monkeypatch.setattr(dl, "_reserve_download", self.reserve)
        monkeypatch.setattr(dl, "_release_download", self.release)
        monkeypatch.setattr(dl, "session_scope", _session_factory(session))
        for platform in ("tiktok", "youtube", "instagram", "twitter"):
            monkeypatch.setitem(dl.DOWNLOADERS, platform, self.downloader)
        monkeypatch.setattr(dl, "_active_users", set())
        monkeypatch.setattr(dl, "_download_slots", asyncio.Semaphore(3))
        monkeypatch.setattr(settings, "free_user_limit", 5)
        monkeypatch.setattr(settings, "premium_user_limit", 999)

    def message(self, text):
        m = MagicMock()
        m.text = text
        m.answer = AsyncMock(return_value=self.processing)
        m.answer_video = AsyncMock()
        m.answer_photo = AsyncMock()
        m.answer_document = AsyncMock()
        return m

    def user(self, user_id=USER_ID, premium=False):
        return SimpleNamespace(id=user_id, is_premium=premium)

    def make_file(self, content_type="video", size=2048):
        """Что делает настоящий загрузчик при успехе: файл в каталоге запроса."""
        workdir = new_workdir("youtube")
        f = workdir / "youtube_abc.mp4"
        f.write_bytes(b"x" * 16)
        return {
            "success": True,
            "file_path": str(f),
            "content_type": content_type,
            "file_size": size,
        }, workdir


@pytest.fixture
def h(monkeypatch, workroot):
    return Harness(monkeypatch, workroot)


def _answered_texts(message):
    return [c.args[0] for c in message.answer.await_args_list]


@pytest.mark.asyncio
async def test_no_user_is_skipped(h):
    m = h.message(URL)
    await dl.handle_url(m, user=None)
    m.answer.assert_not_awaited()
    h.reserve.assert_not_awaited()
    h.downloader.assert_not_awaited()


@pytest.mark.asyncio
async def test_rate_limited_answers_limit_and_downloads_nothing(h):
    m = h.message(URL)
    await dl.handle_url(m, user=h.user(), rate_limited=True)

    assert _answered_texts(m) == [dl.limit_text(False)]
    h.reserve.assert_not_awaited()
    h.downloader.assert_not_awaited()
    h.release.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "привет, как дела",
        "http://evil.example/?a=tiktok.com",
        "ytsearch5:cats",
        "file:///etc/passwd",
    ],
)
async def test_message_without_known_link_is_ignored(h, text):
    m = h.message(text)
    await dl.handle_url(m, user=h.user())

    m.answer.assert_not_awaited()
    h.reserve.assert_not_awaited()
    h.downloader.assert_not_awaited()
    h.release.assert_not_awaited()
    assert dl._active_users == set()


@pytest.mark.asyncio
async def test_platform_without_downloader_is_refused_before_reserving_slot(h):
    m = h.message("https://www.reddit.com/r/x/comments/1/")
    await dl.handle_url(m, user=h.user())

    assert len(_answered_texts(m)) == 1 and "не скачаю" in _answered_texts(m)[0]
    h.reserve.assert_not_awaited()
    h.downloader.assert_not_awaited()
    h.release.assert_not_awaited()


@pytest.mark.asyncio
async def test_exhausted_limit_refuses_without_download_or_release(h):
    h.reserve.return_value = False
    m = h.message(URL)

    await dl.handle_url(m, user=h.user())

    h.reserve.assert_awaited_once_with(USER_ID, 5)
    h.downloader.assert_not_awaited()
    assert _answered_texts(m) == [dl.limit_text(False)]
    # слот не занят - возвращать нечего (иначе счётчик уйдёт ниже реального)
    h.release.assert_not_awaited()
    assert dl._active_users == set()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error", [RuntimeError("db is down"), asyncio.CancelledError()]
)
async def test_reserve_failure_frees_user_and_downloads_nothing(h, error):
    h.reserve.side_effect = error
    m = h.message(URL)

    with pytest.raises(type(error)):
        await dl.handle_url(m, user=h.user())

    # пользователь не остаётся "занятым" навсегда, слот не занят - возвращать нечего
    assert dl._active_users == set()
    h.downloader.assert_not_awaited()
    h.release.assert_not_awaited()


@pytest.mark.asyncio
async def test_second_request_is_refused_while_first_is_still_reserving_slot(h):
    """Проверка "занят" и отметка стоят до await в _reserve_download: иначе две ссылки подряд пройдут обе."""
    started = asyncio.Event()
    gate = asyncio.Event()

    async def slow_reserve(user_id, limit):
        started.set()
        await gate.wait()
        return True

    h.reserve.side_effect = slow_reserve
    h.downloader.return_value = {"success": False, "error": "x"}
    first = h.message(URL)
    second = h.message("https://youtu.be/second")

    task = asyncio.create_task(dl.handle_url(first, user=h.user()))
    await asyncio.wait_for(started.wait(), 5)

    await asyncio.wait_for(dl.handle_url(second, user=h.user()), 5)

    assert "Ещё качаю" in _answered_texts(second)[0]
    assert h.reserve.await_count == 1
    gate.set()
    await asyncio.wait_for(task, 5)
    h.downloader.assert_awaited_once_with(URL)
    assert dl._active_users == set()


@pytest.mark.asyncio
async def test_premium_user_reserves_against_premium_limit(h):
    h.downloader.return_value = {"success": False, "error": "x"}
    await dl.handle_url(h.message(URL), user=h.user(premium=True))
    h.reserve.assert_awaited_once_with(USER_ID, 999)


@pytest.mark.asyncio
async def test_failed_download_releases_slot_and_escapes_error(h):
    h.downloader.return_value = {
        "success": False,
        "error": "<b>boom</b> & <script>alert(1)</script>",
    }
    m = h.message(URL)

    await dl.handle_url(m, user=h.user())

    h.reserve.assert_awaited_once()
    h.release.assert_awaited_once_with(USER_ID)
    h.processing.edit_text.assert_awaited_once()
    text = h.processing.edit_text.await_args.args[0]
    assert "&lt;b&gt;boom&lt;/b&gt; &amp; &lt;script&gt;" in text
    assert "<b>boom" not in text and "<script>" not in text
    m.answer_video.assert_not_awaited()
    assert dl._active_users == set()


@pytest.mark.asyncio
async def test_failed_download_without_error_text_gets_default_message(h):
    h.downloader.return_value = {"success": False}
    await dl.handle_url(h.message(URL), user=h.user())

    assert "неизвестная ошибка" in h.processing.edit_text.await_args.args[0]
    h.release.assert_awaited_once_with(USER_ID)


@pytest.mark.asyncio
async def test_downloader_receives_only_the_url_not_the_whole_text(h):
    h.downloader.return_value = {"success": False, "error": "x"}
    await dl.handle_url(
        h.message("смотри https://youtu.be/abc, класс\nhttp://169.254.169.254/"),
        user=h.user(),
    )
    h.downloader.assert_awaited_once_with("https://youtu.be/abc")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content_type, method",
    [
        ("video", "answer_video"),
        ("photo", "answer_photo"),
        ("document", "answer_document"),
    ],
)
async def test_success_delivers_file_removes_workdir_and_does_not_release(
    h, content_type, method
):
    result, workdir = h.make_file(content_type)
    h.downloader.return_value = result
    m = h.message("смотри https://youtu.be/abc, класс")

    await dl.handle_url(m, user=h.user())

    getattr(m, method).assert_awaited_once()
    for other in {"answer_video", "answer_photo", "answer_document"} - {method}:
        getattr(m, other).assert_not_awaited()
    h.release.assert_not_awaited()
    assert not workdir.exists()
    h.processing.delete.assert_awaited_once()
    assert dl._active_users == set()

    # в историю пишется только URL, а не весь текст сообщения
    assert len(h.added) == 1
    rec = h.added[0]
    assert isinstance(rec, Download)
    assert rec.url == "https://youtu.be/abc"
    assert rec.platform == "youtube"
    assert rec.user_id == USER_ID
    assert rec.content_type == content_type
    assert rec.status == "completed"
    assert h.fresh_user.total_downloads == 11
    h.session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_too_big_file_releases_slot_and_removes_workdir(h, monkeypatch):
    monkeypatch.setattr(settings, "max_file_size_mb", 1)
    result, workdir = h.make_file(size=5 * 1024 * 1024)
    h.downloader.return_value = result
    m = h.message(URL)

    await dl.handle_url(m, user=h.user())

    assert "слишком большой" in h.processing.edit_text.await_args.args[0]
    m.answer_video.assert_not_awaited()
    h.release.assert_awaited_once_with(USER_ID)
    assert not workdir.exists()
    assert h.added == []


@pytest.mark.asyncio
async def test_telegram_failure_on_send_releases_slot_and_removes_workdir(h):
    result, workdir = h.make_file()
    h.downloader.return_value = result
    m = h.message(URL)
    m.answer_video.side_effect = RuntimeError("telegram is down")

    await dl.handle_url(m, user=h.user())  # исключение наружу не выходит

    assert "Что-то пошло не так" in h.processing.edit_text.await_args.args[0]
    h.release.assert_awaited_once_with(USER_ID)
    assert not workdir.exists()
    assert h.added == []
    assert dl._active_users == set()


@pytest.mark.asyncio
async def test_downloader_exception_releases_slot_and_frees_user(h):
    h.downloader.side_effect = RuntimeError("boom")
    m = h.message(URL)

    await dl.handle_url(m, user=h.user())

    assert "Что-то пошло не так" in h.processing.edit_text.await_args.args[0]
    h.release.assert_awaited_once_with(USER_ID)
    assert dl._active_users == set()


@pytest.mark.asyncio
async def test_error_after_delivery_does_not_release_slot(h):
    """Файл уже у пользователя: сбой записи статистики не должен возвращать скачивание."""
    result, workdir = h.make_file()
    h.downloader.return_value = result
    h.session.execute.side_effect = RuntimeError("db is down")
    m = h.message(URL)

    await dl.handle_url(m, user=h.user())

    m.answer_video.assert_awaited_once()
    h.release.assert_not_awaited()
    assert not workdir.exists()
    assert dl._active_users == set()


@pytest.mark.asyncio
async def test_second_request_of_same_user_is_refused_while_first_runs(h):
    started = asyncio.Event()
    gate = asyncio.Event()

    async def slow(url):
        started.set()
        await gate.wait()
        return {"success": False, "error": "done"}

    h.downloader.side_effect = slow
    first = h.message(URL)
    second = h.message("https://youtu.be/second")
    other_user_msg = h.message("https://youtu.be/third")

    task = asyncio.create_task(dl.handle_url(first, user=h.user()))
    await asyncio.wait_for(started.wait(), 5)
    assert USER_ID in dl._active_users

    # без замка второй запрос застрял бы на gate: ограничиваем ожидание, а не вешаем набор
    await asyncio.wait_for(dl.handle_url(second, user=h.user()), 5)

    assert (
        len(_answered_texts(second)) == 1 and "Ещё качаю" in _answered_texts(second)[0]
    )
    # отказ не съедает слот лимита и не запускает загрузку
    h.reserve.assert_awaited_once_with(USER_ID, 5)
    h.downloader.assert_awaited_once_with(URL)
    h.release.assert_not_awaited()
    assert USER_ID in dl._active_users  # отказ не освободил чужой замок

    # другой пользователь не блокируется
    started.clear()
    other = asyncio.create_task(dl.handle_url(other_user_msg, user=h.user(user_id=456)))
    await asyncio.wait_for(started.wait(), 5)
    assert h.reserve.await_count == 2

    gate.set()
    await asyncio.wait_for(asyncio.gather(task, other), 5)
    assert dl._active_users == set()

    # и после завершения первый пользователь снова может качать
    h.downloader.side_effect = None
    h.downloader.return_value = {"success": False, "error": "again"}
    again = h.message(URL)
    await dl.handle_url(again, user=h.user())
    assert "Ещё качаю" not in " ".join(_answered_texts(again))
    assert h.reserve.await_count == 3
