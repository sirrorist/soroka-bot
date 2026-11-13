"""Private AI conversation: routing, history window, drafts, stop and failures."""

import json
import threading
import time
from datetime import timedelta
from types import SimpleNamespace

import pytest

from sobriety.api import ApiError
from sobriety.bot import Bot
from sobriety.llm import ChatService, LLMConfig, RouterError, split, stream, window
from sobriety.settings import Binding, Settings, Subject
from sobriety.store import CHAT_KEEP, CHAT_RETENTION, Store
from tests.test_sobriety import MONDAY, FakeApi, message

NOW = MONDAY + timedelta(days=9)
CONFIG = LLMConfig("https://router.test/v1", "k", "combo", history_chars=100)


class FakeChat:
    def __init__(self, state="ok"):
        self.state = state
        self.submitted = []
        self.stopped = []
        self.done = []

    def submit(self, user_id, chat_id, text, history):
        self.submitted.append((user_id, chat_id, text, history))
        return self.state

    def stop(self, chat_id, draft_id):
        self.stopped.append((chat_id, draft_id))

    def drain(self):
        done, self.done = self.done, []
        return done


@pytest.fixture
def app(tmp_path):
    store = Store(tmp_path / "tracker.sqlite3")
    settings = Settings(
        "0:fake",
        (Subject(101, "alice_test"), Subject(202, "bravo_test")),
        (Binding(101, -100, 7), Binding(202, -200, 9)),
    )
    api, chat = FakeApi(), FakeChat()
    yield SimpleNamespace(
        bot=Bot(settings, store, api, "tracker_bot", lambda: NOW, chat=chat),
        api=api,
        store=store,
        chat=chat,
    )
    store.close()


def send(app, update_id=1, **kwargs):
    app.bot.handle({"update_id": update_id, "message": message(**kwargs)})


def texts(app):
    return [f["text"] for m, f in app.api.calls if m == "sendMessage"]


def test_private_text_of_subject_goes_to_model_with_stored_history(app):
    app.store.add_chat_turn(101, "Привет", "Здравствуй", NOW)
    send(app, text="Как дела?")
    assert app.chat.submitted == [
        (
            101,
            101,
            "Как дела?",
            [
                {"role": "user", "content": "Привет"},
                {"role": "assistant", "content": "Здравствуй"},
            ],
        )
    ]
    assert texts(app) == []


def test_finished_turn_is_stored_before_next_prompt(app):
    app.chat.done = [(101, "раз", "ответ")]
    send(app, text="два")
    assert app.chat.submitted[0][3][-1] == {"role": "assistant", "content": "ответ"}


@pytest.mark.parametrize(
    "kwargs",
    [
        {"actor": 303, "chat": 303, "text": "чужой"},  # not in SOBRIETY_USERS
        {"actor": 101, "chat": -100, "thread": 7, "text": "в группе"},
    ],
)
def test_model_is_private_and_subjects_only(app, kwargs):
    send(app, **kwargs)
    assert app.chat.submitted == []


def test_active_form_has_priority_over_model(app):
    app.bot.prompt(101, message(), "message", 101, "Введите:")
    send(app, text="мой текст")
    assert app.chat.submitted == []


def test_late_reply_to_expired_form_is_not_a_prompt(app):
    send(app, text="2026-10-01", reply_to_message={"from": {"id": 9, "is_bot": True}})
    assert app.chat.submitted == []
    assert "форма уже закрыта" in texts(app)[-1]


@pytest.mark.parametrize("state,expected", [("busy", "ещё отвечаю"), ("full", "много")])
def test_busy_or_full_queue_is_explained(app, state, expected):
    app.chat.state = state
    send(app, text="?")
    assert expected in texts(app)[-1]


def test_new_command_clears_history(app):
    app.store.add_chat_turn(101, "a", "b", NOW)
    send(app, text="/new")
    assert app.store.chat_history(101, NOW) == []
    assert "новый разговор" in texts(app)[-1]


def test_new_is_offered_only_in_private_menu_of_subject(app):
    send(app, text="/start")
    commands = [f for m, f in app.api.calls if m == "setMyCommands"][-1]["commands"]
    assert [c["command"] for c in commands] == ["sober", "new"]
    assert [c["command"] for c in app.bot.commands()] == ["sober"]


def test_stop_update_reaches_chat_service(app):
    app.bot.handle(
        {
            "update_id": 5,
            "stopped_message_generation": {"chat": {"id": 101}, "draft_id": 77},
        }
    )
    assert app.chat.stopped == [(101, 77)]


def test_history_is_trimmed_and_expires(tmp_path):
    store = Store(tmp_path / "s.sqlite3")
    for i in range(CHAT_KEEP):
        store.add_chat_turn(101, f"q{i}", f"a{i}", NOW)
    history = store.chat_history(101, NOW)
    assert len(history) == CHAT_KEEP and history[-1]["content"] == f"a{CHAT_KEEP - 1}"
    later = NOW + timedelta(seconds=CHAT_RETENTION + 1)
    assert store.chat_history(101, later) == []
    store.close()


def test_window_keeps_newest_turns_within_budget_starting_with_user():
    history = [
        {"role": "user", "content": "x" * 60},
        {"role": "assistant", "content": "y" * 30},
        {"role": "user", "content": "z" * 30},
        {"role": "assistant", "content": "w" * 30},
    ]
    assert [m["content"][0] for m in window(history, 100)] == ["z", "w"]
    assert window(history, 0) == []


def test_split_respects_telegram_limit():
    parts = split("слово " * 2000)
    assert len(parts) > 1 and all(len(p) <= 4096 for p in parts)


def test_config_is_off_without_variables_and_strict_when_partial(monkeypatch):
    for name in ("BASE_URL", "API_KEY", "MODEL"):
        monkeypatch.delenv("SOBRIETY_LLM_" + name, raising=False)
    assert LLMConfig.from_env() is None
    monkeypatch.setenv("SOBRIETY_LLM_MODEL", "combo")
    with pytest.raises(ValueError):
        LLMConfig.from_env()
    monkeypatch.setenv("SOBRIETY_LLM_API_KEY", "k")
    monkeypatch.setenv("SOBRIETY_LLM_BASE_URL", "http://plain.test/v1")
    with pytest.raises(ValueError):
        LLMConfig.from_env()
    monkeypatch.setenv("SOBRIETY_LLM_BASE_URL", "https://router.test/v1/")
    assert LLMConfig.from_env().base_url == "https://router.test/v1"


class RecordingApi:
    def __init__(self, fail=None):
        self.calls, self.fail = [], fail

    def call(self, method, fields=None, **kwargs):
        self.calls.append((method, dict(fields)))
        if self.fail and method == self.fail[0]:
            raise self.fail[1]
        return True


def run(streamer, api=None, text="вопрос"):
    api = api or RecordingApi()
    service = ChatService(CONFIG, "0:x", api=api, streamer=streamer)
    service.start()
    assert service.submit(101, 101, text, []) == "ok"
    deadline = time.monotonic() + 5
    while service.busy(101) and time.monotonic() < deadline:
        time.sleep(0.01)
    service.shutdown()
    return api, service.drain()


def test_answer_streams_into_one_draft_then_final_message():
    def streamer(config, messages, stop):
        assert messages[0]["role"] == "system" and messages[-1]["content"] == "вопрос"
        yield "Привет"
        time.sleep(0.45)
        yield ", мир"

    api, done = run(streamer)
    drafts = [f for m, f in api.calls if m == "sendMessageDraft"]
    assert drafts[0]["text"] == "" and len({d["draft_id"] for d in drafts}) == 1
    assert all(d["can_stop"] for d in drafts)
    assert [f["text"] for m, f in api.calls if m == "sendMessage"] == ["Привет, мир"]
    assert done == [(101, "вопрос", "Привет, мир")]


def test_router_failure_without_text_is_reported_and_not_stored():
    def streamer(config, messages, stop):
        raise RouterError("HTTP 503")
        yield  # pragma: no cover

    api, done = run(streamer)
    assert "Не получилось" in [f for m, f in api.calls if m == "sendMessage"][0]["text"]
    assert done == []


def test_draft_rate_limit_does_not_break_the_answer():
    def streamer(config, messages, stop):
        yield "текст"

    api, done = run(streamer, RecordingApi(("sendMessageDraft", ApiError(429, 3))))
    assert done == [(101, "вопрос", "текст")]


def test_one_answer_per_user_and_stop_keeps_partial_answer():
    started = threading.Event()

    def streamer(config, messages, stop):
        yield "часть"
        started.set()
        while not stop.is_set():
            time.sleep(0.01)

    api = RecordingApi()
    service = ChatService(CONFIG, "0:x", api=api, streamer=streamer)
    service.start()
    service.submit(101, 101, "вопрос", [])
    started.wait(2)
    assert service.submit(101, 101, "ещё", []) == "busy"
    draft = [f for m, f in api.calls if m == "sendMessageDraft"][0]["draft_id"]
    assert not service.stop(101, draft + 1)
    assert service.stop(101, draft)
    while service.busy(101):
        time.sleep(0.01)
    assert service.drain() == [(101, "вопрос", "часть …")]
    service.shutdown()


def set_router_env(monkeypatch, effort=None):
    monkeypatch.setenv("SOBRIETY_LLM_BASE_URL", "https://router.test/v1")
    monkeypatch.setenv("SOBRIETY_LLM_API_KEY", "k")
    monkeypatch.setenv("SOBRIETY_LLM_MODEL", "combo")
    if effort is None:
        monkeypatch.delenv("SOBRIETY_LLM_REASONING_EFFORT", raising=False)
    else:
        monkeypatch.setenv("SOBRIETY_LLM_REASONING_EFFORT", effort)


def sent_body(config):
    class Opener:
        def open(self, request, timeout=None):
            self.body = json.loads(request.data)
            raise OSError("offline")

    opener = Opener()
    with pytest.raises(RouterError):
        list(stream(config, [], threading.Event(), opener))
    return opener.body


@pytest.mark.parametrize("effort,expected", [(None, "low"), ("high", "high")])
def test_reasoning_effort_is_sent(monkeypatch, effort, expected):
    set_router_env(monkeypatch, effort)
    assert sent_body(LLMConfig.from_env())["reasoning_effort"] == expected


def test_reasoning_effort_off_omits_the_key(monkeypatch):
    set_router_env(monkeypatch, "off")
    assert "reasoning_effort" not in sent_body(LLMConfig.from_env())


def test_bad_reasoning_effort_is_rejected(monkeypatch):
    set_router_env(monkeypatch, "max")
    with pytest.raises(ValueError):
        LLMConfig.from_env()
