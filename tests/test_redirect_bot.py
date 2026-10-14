"""redirect_bot.py: заглушка старого бота.

Модуль читает OLD_BOT_TOKEN и NEW_BOT_USERNAME из окружения при импорте, поэтому каждый
тест импортирует его заново (фикстура rb) с нужным окружением. Telegram не нужен:
вызовы Bot API либо подменены записью, либо идут в локальный HTTP-сервер, время -
подставные часы.
"""

import email.parser
import email.policy
import importlib
import json
import signal
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


def load(monkeypatch, token="1:old", username="@Soroka_bot", photo=None):
    for name, value in (
        ("OLD_BOT_TOKEN", token),
        ("NEW_BOT_USERNAME", username),
        ("MOVED_PHOTO", photo),
    ):
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
        self.slept = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


class Recorder:
    """Подмена _api: запоминает (метод, поля, картинка), отвечает по очереди из replies."""

    def __init__(self, replies=None):
        self.calls = []
        self.replies = replies or {}

    def __call__(self, method, fields=None, photo=None, timeout=15):
        self.calls.append((method, dict(fields or {}), photo))
        queue = self.replies.get(method)
        if queue:
            reply = queue.pop(0)
            if isinstance(reply, BaseException):
                raise reply
            return reply
        return True

    def methods(self):
        return [m for m, _, _ in self.calls]


@pytest.fixture
def api(rb, monkeypatch):
    recorder = Recorder()
    monkeypatch.setattr(rb, "_api", recorder)
    return recorder


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


def test_no_heavy_packages_are_imported():
    """Смысл заглушки - память: aiogram и pydantic тянут ~150 МБ."""
    code = (
        "import sys, redirect_bot\n"
        "heavy = ('aiogram', 'pydantic', 'aiohttp', 'loguru', 'sqlalchemy', 'asyncio')\n"
        "print(sorted({m.split('.')[0] for m in sys.modules} & set(heavy)))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        env={"OLD_BOT_TOKEN": "1:x", "NEW_BOT_USERNAME": "x", "PYTHONPATH": "."},
        cwd=Path(__file__).resolve().parent.parent,
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.strip() == "[]"


# ---------- тексты ----------


def test_texts_have_same_keys_in_both_languages_and_mention_new_bot(rb):
    assert (
        set(rb.TEXTS["ru"])
        == set(rb.TEXTS["en"])
        == {"moved", "button", "about", "description"}
    )
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
    assert kb == {
        "inline_keyboard": [
            [{"text": rb.TEXTS[lang]["button"], "url": "https://t.me/Soroka_bot"}]
        ]
    }


# ---------- _lang ----------


@pytest.mark.parametrize(
    "code, expected",
    [
        ("ru", "ru"),
        ("RU", "ru"),
        ("ru-RU", "ru"),
        ("uk", "ru"),
        ("uk-UA", "ru"),
        ("be", "ru"),
        ("kk", "ru"),
        ("en", "en"),
        ("en-US", "en"),
        ("de", "en"),
        ("", "en"),
        (None, "en"),
    ],
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


def test_due_prunes_entries_older_than_longest_window(rb, monkeypatch):
    clock = Clock(now=10_000.0)
    monkeypatch.setattr(rb, "time", clock)
    for chat in range(1, 101):
        rb._due(chat, "private")
    assert len(rb._last) == 100
    clock.now += 3599
    rb._due(-1, "group")  # чистка ещё не пришла: прошлая была на первом вызове
    assert len(rb._last) == 101
    clock.now += 1
    rb._due(-2, "group")  # час с прошлой чистки: старые записи ушли
    assert set(rb._last) == {-1, -2}


def test_due_prune_keeps_the_window_of_a_live_entry(rb, monkeypatch):
    clock = Clock(now=10_000.0)
    monkeypatch.setattr(rb, "time", clock)
    rb._due(-1, "group")
    clock.now += 3599
    rb._due(5, "private")
    clock.now += 1
    rb._due(6, "private")  # чистка; группе -1 ровно час - её окно кончилось
    assert -1 not in rb._last
    assert 5 in rb._last  # частная запись моложе часа осталась
    assert rb._due(5, "private") is False  # и её минута по-прежнему действует


# ---------- _send_moved ----------


def test_send_moved_without_photo_sends_text_with_button(rb, api):
    assert not rb.PHOTO.is_file()
    rb._send_moved(1, "en")
    [(method, fields, photo)] = api.calls
    assert method == "sendMessage"
    assert photo is None
    assert fields["chat_id"] == 1
    assert fields["text"] == rb.TEXTS["en"]["moved"]
    assert fields["parse_mode"] == "HTML"
    assert fields["link_preview_options"] == {"is_disabled": True}
    assert fields["reply_markup"] == rb._keyboard("en")
    assert fields["message_thread_id"] is None


def test_send_moved_into_topic_passes_thread_id(rb, api):
    rb._send_moved(-100, "ru", thread_id=7)
    assert api.calls[0][1]["message_thread_id"] == 7


@pytest.fixture
def with_photo(monkeypatch, tmp_path):
    picture = tmp_path / "moved.png"
    picture.write_bytes(b"\x89PNG fake")
    mod = load(monkeypatch, photo=str(picture))
    recorder = Recorder()
    monkeypatch.setattr(mod, "_api", recorder)
    yield mod, recorder, picture
    sys.modules.pop("redirect_bot", None)


def sent_photo(file_id):
    return {"photo": [{"file_id": "small"}, {"file_id": file_id}]}


def test_photo_is_uploaded_once_then_sent_by_file_id(with_photo):
    rb, api, picture = with_photo
    api.replies["sendPhoto"] = [sent_photo("F1")]
    rb._send_moved(1, "en")
    rb._send_moved(2, "ru")
    (m1, f1, p1), (m2, f2, p2) = api.calls
    assert (m1, p1) == ("sendPhoto", picture)  # первый раз - файлом
    assert "photo" not in f1
    assert f1["caption"] == rb.TEXTS["en"]["moved"]
    assert (m2, p2) == ("sendPhoto", None)  # дальше - по file_id самого крупного
    assert f2["photo"] == "F1"
    assert f2["caption"] == rb.TEXTS["ru"]["moved"]


def test_rejected_file_id_is_replaced_by_new_upload(with_photo):
    rb, api, picture = with_photo
    rb._photo_id = "STALE"
    api.replies["sendPhoto"] = [
        rb.ApiError(400, "wrong file identifier", None),
        sent_photo("F2"),
    ]
    rb._send_moved(1, "en")
    assert [p for _, _, p in api.calls] == [None, picture]
    assert rb._photo_id == "F2"


def test_other_errors_on_file_id_are_not_retried(with_photo):
    rb, api, _ = with_photo
    rb._photo_id = "F1"
    api.replies["sendPhoto"] = [rb.ApiError(403, "blocked", None)]
    with pytest.raises(rb.ApiError):
        rb._send_moved(1, "en")
    assert len(api.calls) == 1
    assert rb._photo_id == "F1"


# ---------- handle: маршрутизация ----------


@pytest.fixture
def routed(rb, monkeypatch):
    """Вместо отправки - запись (чат, язык, топик)."""
    monkeypatch.setattr(rb, "time", Clock())
    sent = []
    monkeypatch.setattr(
        rb,
        "_send_moved",
        lambda chat, lang, thread=None: sent.append((chat, lang, thread)),
    )
    return sent


def message(chat_id, chat_type, text=None, lang_code=None, **extra):
    msg = {
        "message_id": 1,
        "chat": {"id": chat_id, "type": chat_type},
        "from": {"id": 9, "is_bot": False, "first_name": "t"},
        **extra,
    }
    if text is not None:
        msg["text"] = text
    if lang_code is not None:
        msg["from"]["language_code"] = lang_code
    return {"update_id": 1, "message": msg}


def test_private_message_gets_notice_in_users_language(rb, routed):
    rb.handle(message(1, "private", "привет", lang_code="uk"))
    rb.handle(message(2, "private", "hi", lang_code="en"))
    assert routed == [(1, "ru", None), (2, "en", None)]


def test_private_message_without_text_still_gets_notice(rb, routed):
    rb.handle(message(1, "private", photo=[{"file_id": "x"}]))
    assert routed == [(1, "en", None)]


def test_private_repeat_within_a_minute_is_silent(rb, routed):
    rb.handle(message(1, "private", "a"))
    rb.handle(message(1, "private", "b"))
    assert routed == [(1, "en", None)]


@pytest.mark.parametrize("chat_type", ["group", "supergroup"])
def test_group_answers_commands_only(rb, routed, chat_type):
    rb.handle(message(-100, chat_type, "просто текст", lang_code="ru"))
    rb.handle(message(-100, chat_type, None, lang_code="ru"))
    assert routed == []
    rb.handle(message(-100, chat_type, "/start", lang_code="ru"))
    assert routed == [(-100, "ru", None)]


def test_group_command_repeat_within_an_hour_is_silent(rb, routed):
    rb.handle(message(-100, "supergroup", "/start"))
    rb.handle(message(-100, "supergroup", "/help"))
    assert len(routed) == 1


def test_topic_command_is_answered_in_the_same_topic(rb, routed):
    rb.handle(
        message(-100, "supergroup", "/dl", message_thread_id=42, is_topic_message=True)
    )
    assert routed == [(-100, "en", 42)]


def test_reply_thread_outside_topics_is_not_used(rb, routed):
    # message_thread_id без is_topic_message - ветка ответов, а не топик
    rb.handle(message(-100, "supergroup", "/dl", message_thread_id=42))
    assert routed == [(-100, "en", None)]


def test_private_and_group_cooldowns_do_not_block_each_other(rb, routed):
    rb.handle(message(1, "private", "x"))
    rb.handle(message(-100, "group", "/start"))
    assert [c for c, _, _ in routed] == [1, -100]


def test_message_without_sender_is_answered_in_english(rb, routed):
    update = message(-100, "supergroup", "/start")
    del update["message"]["from"]
    rb.handle(update)
    assert routed == [(-100, "en", None)]


def test_other_update_kinds_are_ignored(rb, routed, api):
    rb.handle({"update_id": 1, "edited_message": message(1, "private", "x")["message"]})
    assert routed == [] and api.calls == []


def test_old_button_shows_alert_with_link_text(rb, api):
    rb.handle(
        {
            "update_id": 1,
            "callback_query": {"id": "q1", "from": {"id": 9, "language_code": "ru"}},
        }
    )
    rb.handle({"update_id": 2, "callback_query": {"id": "q2", "from": {"id": 10}}})
    assert api.calls == [
        (
            "answerCallbackQuery",
            {
                "callback_query_id": "q1",
                "text": rb.TEXTS["ru"]["about"],
                "show_alert": True,
            },
            None,
        ),
        (
            "answerCallbackQuery",
            {
                "callback_query_id": "q2",
                "text": rb.TEXTS["en"]["about"],
                "show_alert": True,
            },
            None,
        ),
    ]


# ---------- профиль бота ----------


def test_setup_profile_sets_both_languages_and_clears_menu(rb, api):
    rb._setup_profile()
    short = {
        f.get("language_code"): f["short_description"]
        for m, f, _ in api.calls
        if m == "setMyShortDescription"
    }
    full = {
        f.get("language_code"): f["description"]
        for m, f, _ in api.calls
        if m == "setMyDescription"
    }
    cleared = [
        f.get("language_code") for m, f, _ in api.calls if m == "deleteMyCommands"
    ]
    assert short == {None: rb.TEXTS["en"]["about"], "ru": rb.TEXTS["ru"]["about"]}
    assert full == {
        None: rb.TEXTS["en"]["description"],
        "ru": rb.TEXTS["ru"]["description"],
    }
    assert cleared == [None, "ru"]


# ---------- poll ----------


class Stop(Exception):
    pass


def updates(*ids):
    return [message(1, "private", "x") | {"update_id": i} for i in ids]


@pytest.fixture
def polling(rb, monkeypatch):
    clock = Clock()
    monkeypatch.setattr(rb, "time", clock)
    handled = []
    monkeypatch.setattr(rb, "handle", lambda u: handled.append(u["update_id"]))
    recorder = Recorder()
    monkeypatch.setattr(rb, "_api", recorder)
    return recorder, handled, clock


def offsets(api):
    return [f.get("offset") for m, f, _ in api.calls if m == "getUpdates"]


def test_poll_confirms_offset_after_each_batch(rb, polling):
    api, handled, _ = polling
    api.replies["getUpdates"] = [updates(5, 6), updates(7), Stop()]
    with pytest.raises(Stop):
        rb.poll()
    assert handled == [5, 6, 7]
    assert offsets(api) == [None, 7, 8]
    first = api.calls[0][1]
    assert first["timeout"] == rb.POLL_TIMEOUT_S
    assert first["allowed_updates"] == ["message", "callback_query"]


def test_poll_waits_retry_after_on_flood_control(rb, polling):
    api, handled, clock = polling
    api.replies["getUpdates"] = [
        rb.ApiError(429, "Too Many Requests", 17),
        updates(1),
        Stop(),
    ]
    with pytest.raises(Stop):
        rb.poll()
    assert clock.slept == [17]
    assert handled == [1]


def test_poll_backs_off_on_network_errors_and_resets(rb, polling):
    api, handled, clock = polling
    api.replies["getUpdates"] = [
        OSError("down"),
        TimeoutError(),
        OSError("down"),
        updates(1),
        OSError("down"),
        Stop(),
    ]
    with pytest.raises(Stop):
        rb.poll()
    assert clock.slept == [1, 2, 4, 1]  # после удачного запроса - снова с секунды


def test_poll_backoff_is_capped(rb, polling):
    api, _, clock = polling
    api.replies["getUpdates"] = [OSError()] * 10 + [Stop()]
    with pytest.raises(Stop):
        rb.poll()
    assert max(clock.slept) == rb.MAX_BACKOFF_S


def test_poll_conflict_is_retried(rb, polling):
    # 409 - второй getUpdates или не снятый webhook: подождать и повторить
    api, _, clock = polling
    api.replies["getUpdates"] = [rb.ApiError(409, "Conflict", None), Stop()]
    with pytest.raises(Stop):
        rb.poll()
    assert clock.slept == [1]


@pytest.mark.parametrize("code", [401, 404])
def test_poll_stops_on_bad_token(rb, polling, code):
    api, _, _ = polling
    api.replies["getUpdates"] = [rb.ApiError(code, "Unauthorized", None)]
    with pytest.raises(rb.ApiError):
        rb.poll()


def test_failed_reply_does_not_stop_polling_or_repeat_update(rb, monkeypatch):
    clock = Clock()
    monkeypatch.setattr(rb, "time", clock)
    seen = []

    def handle(update):
        seen.append(update["update_id"])
        errors = {
            1: rb.ApiError(403, "blocked", None),
            2: rb.ApiError(429, "flood", 5),
            3: OSError("reset"),
            4: KeyError("chat"),
        }
        if update["update_id"] in errors:
            raise errors[update["update_id"]]

    monkeypatch.setattr(rb, "handle", handle)
    api = Recorder({"getUpdates": [updates(1, 2, 3, 4, 5), Stop()]})
    monkeypatch.setattr(rb, "_api", api)
    with pytest.raises(Stop):
        rb.poll()
    assert seen == [1, 2, 3, 4, 5]
    assert offsets(api) == [None, 6]
    assert clock.slept == [5]


# ---------- main ----------


def test_main_drops_webhook_sets_profile_and_handles_sigterm(rb, monkeypatch):
    api = Recorder()
    monkeypatch.setattr(rb, "_api", api)
    monkeypatch.setattr(rb, "poll", lambda: None)
    previous = signal.getsignal(signal.SIGTERM)
    try:
        rb.main()
        assert signal.getsignal(signal.SIGTERM) is rb._stop
    finally:
        signal.signal(signal.SIGTERM, previous)
    assert api.calls[0] == ("deleteWebhook", {"drop_pending_updates": False}, None)
    assert "setMyDescription" in api.methods()
    with pytest.raises(SystemExit):
        rb._stop(signal.SIGTERM, None)


def test_main_survives_profile_failure(rb, monkeypatch):
    api = Recorder({"setMyShortDescription": [OSError("down")]})
    monkeypatch.setattr(rb, "_api", api)
    started = []
    monkeypatch.setattr(rb, "poll", lambda: started.append(True))
    previous = signal.getsignal(signal.SIGTERM)
    try:
        rb.main()
    finally:
        signal.signal(signal.SIGTERM, previous)
    assert started == [True]


# ---------- _api против локального сервера ----------


class FakeBotApi(BaseHTTPRequestHandler):
    """Отвечает по пути запроса; запоминает метод, путь, тип и тело."""

    seen = []
    replies = {}

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        FakeBotApi.seen.append((self.path, self.headers["Content-Type"], body))
        status, payload = FakeBotApi.replies.get(
            self.path.rsplit("/", 1)[-1], (200, {"ok": True, "result": True})
        )
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


@pytest.fixture
def server(rb, monkeypatch):
    FakeBotApi.seen, FakeBotApi.replies = [], {}
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeBotApi)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    monkeypatch.setattr(rb, "API", f"http://127.0.0.1:{httpd.server_port}")
    yield FakeBotApi
    httpd.shutdown()
    httpd.server_close()


def test_api_posts_json_and_returns_result(rb, server):
    server.replies["getMe"] = (200, {"ok": True, "result": {"id": 1}})
    assert rb._api("getMe", {"a": 1, "skip": None}) == {"id": 1}
    [(path, ctype, body)] = server.seen
    assert path == "/bot1:old/getMe"
    assert ctype == "application/json"
    assert json.loads(body) == {"a": 1}  # None не отправляется


def test_api_error_carries_code_and_retry_after(rb, server):
    server.replies["sendMessage"] = (
        429,
        {
            "ok": False,
            "error_code": 429,
            "description": "Too Many Requests: retry after 3",
            "parameters": {"retry_after": 3},
        },
    )
    with pytest.raises(rb.ApiError) as err:
        rb._api("sendMessage", {"chat_id": 1})
    assert err.value.code == 429
    assert err.value.retry_after == 3
    assert "1:old" not in str(err.value)  # токен - в адресе, в тексте ошибки его нет


def test_api_error_without_json_body(rb, server):
    server.replies["getUpdates"] = (502, b"<html>Bad Gateway</html>")
    with pytest.raises(rb.ApiError) as err:
        rb._api("getUpdates")
    assert err.value.code == 502
    assert "1:old" not in str(err.value)


def test_api_network_error_is_oserror(rb, monkeypatch):
    # порт, на котором никто не слушает
    monkeypatch.setattr(rb, "API", "http://127.0.0.1:9")
    with pytest.raises(OSError):
        rb._api("getMe", timeout=2)


def test_api_uploads_photo_as_multipart(rb, server, tmp_path):
    picture = tmp_path / "moved.png"
    picture.write_bytes(b"\x89PNG\r\n--not-a-boundary\r\n\x00\xff")
    server.replies["sendPhoto"] = (200, {"ok": True, "result": sent_photo("F")})
    fields = {
        "chat_id": -100,
        "caption": "<b>Привет</b>",
        "reply_markup": {"inline_keyboard": [[{"text": "a", "url": "b"}]]},
        "message_thread_id": None,
    }
    assert rb._api("sendPhoto", fields, photo=picture) == sent_photo("F")
    [(_, ctype, body)] = server.seen
    msg = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
        b"Content-Type: " + ctype.encode() + b"\r\n\r\n" + body
    )
    parts = {
        p.get_param("name", header="content-disposition"): p for p in msg.iter_parts()
    }
    assert set(parts) == {"chat_id", "caption", "reply_markup", "photo"}
    assert parts["chat_id"].get_content() == "-100"
    assert parts["caption"].get_content() == "<b>Привет</b>"
    assert json.loads(parts["reply_markup"].get_content()) == fields["reply_markup"]
    assert parts["photo"].get_filename() == "moved.png"
    assert parts["photo"].get_payload(decode=True) == picture.read_bytes()


# ---------- находки security-reviewer 2026-10-01 ----------


@pytest.mark.parametrize("token", ["1:SECRETabc def", "1:SECRET\nabc", "SECRETabc", ""])
def test_malformed_token_stops_without_printing_it(monkeypatch, token):
    with pytest.raises(SystemExit) as err:
        load(monkeypatch, token=token)
    sys.modules.pop("redirect_bot", None)
    assert "SECRET" not in str(err.value)


def test_token_whitespace_around_is_stripped(monkeypatch):
    # хвостовой пробел из .env: без strip http.client бросил бы InvalidURL с токеном
    mod = load(monkeypatch, token=" 1:abc \n")
    try:
        assert mod.TOKEN == "1:abc"
    finally:
        sys.modules.pop("redirect_bot", None)


def test_poll_survives_connection_cut_mid_response(rb, polling):
    import http.client

    api, handled, clock = polling
    api.replies["getUpdates"] = [
        http.client.IncompleteRead(b"{"),
        http.client.RemoteDisconnected(),
        updates(1),
        Stop(),
    ]
    with pytest.raises(Stop):
        rb.poll()
    assert clock.slept == [1, 2]
    assert handled == [1]


def test_api_non_object_json_is_api_error(rb, server):
    server.replies["getUpdates"] = (200, [1, 2])
    with pytest.raises(rb.ApiError):
        rb._api("getUpdates")


def test_update_of_unexpected_shape_does_not_stop_polling(rb, monkeypatch):
    monkeypatch.setattr(rb, "time", Clock())
    sent = []
    monkeypatch.setattr(rb, "_send_moved", lambda *a: sent.append(a))
    api = Recorder(
        {
            "getUpdates": [
                [
                    {"update_id": 1, "callback_query": {"id": "1", "from": None}},
                    {"update_id": 2, "message": {"chat": None}},
                    {"no_update_id": True},
                    message(5, "private", "x") | {"update_id": 3},
                ],
                Stop(),
            ]
        }
    )
    monkeypatch.setattr(rb, "_api", api)
    with pytest.raises(Stop):
        rb.poll()
    assert [a[0] for a in sent] == [5]  # нормальный апдейт после кривых обработан
    assert offsets(api) == [None, 4]


def test_chat_side_400_on_file_id_does_not_reupload(with_photo):
    rb, api, _ = with_photo
    rb._photo_id = "F1"
    api.replies["sendPhoto"] = [rb.ApiError(400, "Bad Request: TOPIC_CLOSED", None)]
    with pytest.raises(rb.ApiError):
        rb._send_moved(-100, "en", thread_id=3)
    assert len(api.calls) == 1  # без повторной заливки
    assert rb._photo_id == "F1"


def test_photo_over_telegram_limit_falls_back_to_text(with_photo, monkeypatch):
    rb, api, _ = with_photo
    monkeypatch.setattr(rb, "PHOTO_MAX_BYTES", 4)
    rb._send_moved(1, "en")
    assert api.methods() == ["sendMessage"]


def test_multipart_body_is_streamed_not_built_whole(rb, tmp_path):
    picture = tmp_path / "moved.png"
    picture.write_bytes(b"x" * (200 * 1024))
    body, size, ctype = rb._multipart({"chat_id": 1}, "photo", picture)
    assert not isinstance(body, bytes)  # итератор кусков, файл не читается целиком
    chunks = list(body)
    assert max(len(c) for c in chunks) <= 64 * 1024
    assert sum(len(c) for c in chunks) == size
    assert ctype.startswith("multipart/form-data; boundary=")
