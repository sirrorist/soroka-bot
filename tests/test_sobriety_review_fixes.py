"""Regression scenarios from review and private gift handoff requirements."""

import json
from datetime import timedelta
from types import SimpleNamespace

import pytest

from sobriety.api import ApiError, NetworkError
from sobriety.bot import Bot
from sobriety.store import Store
from tests.test_sobriety import (
    FakeApi,
    cb,
    last_fields,
    message,
    msk,
    open_gift_private,
    reset,
    start,
)
from tests.test_sobriety import (
    app as app,
)
from tests.test_sobriety import (
    store as store,
)

EXPIRED = (
    "Bad Request: query is too old and response timeout expired or query ID is invalid"
)


def test_holiday_keeps_click_day_and_blocks_false_awards_across_seven(app):
    start(app.store)
    before = msk("2026-10-13T06:59")
    app.bot.now = lambda: before
    cb(app, "sb:holiday:101", update_id=1)
    after = msk("2026-10-13T07:01")
    app.bot.now = lambda: after
    app.store.tick(after)
    assert app.store.snapshot(101, after).record == 0
    assert app.store.snapshot(101, after).days == 0
    app.bot.handle({"update_id": 2, "message": message(text="Праздник")})
    assert app.store.holidays(101)[0]["day"] == "2026-10-12"
    assert app.store.snapshot(101, after).record == 0
    app.store.cancel_holiday(101, "2026-10-12", after, "undo")
    assert app.store.snapshot(101, after).record == 1


def test_holiday_is_reserved_before_prompt_delivery_crosses_boundary(app, monkeypatch):
    start(app.store)
    clock = [msk("2026-10-13T06:59")]
    app.bot.now = lambda: clock[0]
    original = app.api.call

    def call(method, fields=None, **kwargs):
        if method == "sendMessage":
            clock[0] = msk("2026-10-13T07:01")
            assert app.store.snapshot(101, clock[0]).record == 0
        return original(method, fields, **kwargs)

    monkeypatch.setattr(app.api, "call", call)
    cb(app, "sb:holiday:101")
    assert app.store.dialog(101, 101, None, clock[0])["payload"]["day"] == "2026-10-12"


def test_abandoned_holiday_reservation_expires_without_input(app):
    start(app.store)
    before = msk("2026-10-13T06:59")
    app.bot.now = lambda: before
    cb(app, "sb:holiday:101")
    expired = before + timedelta(minutes=5)
    app.store.tick(expired)
    assert not app.store.db.execute("SELECT 1 FROM dialogs").fetchone()
    assert app.store.snapshot(101, expired).record == 1


@pytest.mark.parametrize(
    "navigation",
    ["/sober", "/start", "date", "message", "holiday", "holidays", "promise"],
)
def test_private_navigation_invalidates_previous_confirmation(app, navigation):
    start(app.store)
    reset(app.store, app.bot.now())
    cb(app, "sb:reset:101", update_id=10)
    confirmation = last_fields(app)["reply_markup"]["inline_keyboard"][0][0][
        "callback_data"
    ]
    if navigation.startswith("/"):
        app.bot.handle({"update_id": 11, "message": message(text=navigation)})
    else:
        cb(app, f"sb:{navigation}:101", update_id=11)
    assert not app.store.db.execute("SELECT 1 FROM confirmations").fetchone()
    before = app.store.snapshot(101, app.bot.now()).segment_id
    cb(app, confirmation, update_id=12)
    assert app.store.snapshot(101, app.bot.now()).segment_id == before
    assert "устарело" in last_fields(app)["text"]


def test_gift_number_replay_after_restart_cannot_become_description(app):
    open_gift_private(app)
    update = {"update_id": 3, "message": message(303, 303, text="8")}
    app.bot.handle(update)
    assert app.store.offset() is None
    path = app.store.db.execute("PRAGMA database_list").fetchone()[2]
    reopened = Store(path)
    api = FakeApi()
    bot = Bot(app.bot.settings, reopened, api, "tracker_bot", app.bot.now)
    try:
        bot.handle(update)
        assert not reopened.gifts(101)
        dialog = reopened.dialog(303, 303, None, app.bot.now())
        assert dialog["kind"] == "gift_description"
        assert dialog["payload"]["threshold"] == 8
        bot.handle({"update_id": 4, "message": message(303, 303, text="Книга")})
        assert reopened.gifts(101)[0]["description"] == "Книга"
    finally:
        reopened.close()


def test_dialog_transition_and_update_marker_roll_back_together(app, monkeypatch):
    open_gift_private(app)
    original = app.store._remember

    def crash(key, result):
        if key == "message:3":
            raise RuntimeError("simulated stop")
        return original(key, result)

    monkeypatch.setattr(app.store, "_remember", crash)
    update = {"update_id": 3, "message": message(303, 303, text="8")}
    with pytest.raises(RuntimeError):
        app.bot.handle(update)
    assert app.store.dialog(303, 303, None, app.bot.now())["kind"] == "gift_days"
    assert not app.store.processed("message:3")
    monkeypatch.setattr(app.store, "_remember", original)
    app.bot.handle(update)
    assert app.store.dialog(303, 303, None, app.bot.now())["kind"] == "gift_description"
    assert not app.store.gifts(101)


def test_prompt_delivery_retry_keeps_holiday_day_and_has_no_false_award(app):
    start(app.store)
    clock = [msk("2026-10-13T06:59")]
    app.bot.now = lambda: clock[0]
    app.api.failure = NetworkError()
    with pytest.raises(NetworkError):
        cb(app, "sb:holiday:101", update_id=1)
    clock[0] = msk("2026-10-13T07:01")
    app.store.tick(clock[0])
    app.api.failure = None
    cb(app, "sb:holiday:101", update_id=1)
    assert app.store.snapshot(101, clock[0]).record == 0
    assert app.store.dialog(101, 101, None, clock[0])["payload"]["day"] == "2026-10-12"


def final_input(app, kind):
    if kind == "gift_description":
        open_gift_private(app)
        app.bot.handle({"update_id": 3, "message": message(303, 303, text="8")})
        return "add_gift", 303, 4, "Книга"
    if kind == "holiday":
        start(app.store)
    cb(app, f"sb:{'date' if kind == 'date' else kind}:101", update_id=1)
    return (
        {"message": "set_message", "date": "set_start", "holiday": "add_holiday"}[kind],
        101,
        2,
        {"message": "Мой текст", "date": "12.10.2026", "holiday": "Праздник"}[kind],
    )


@pytest.mark.parametrize("kind", ["gift_description", "message", "date", "holiday"])
def test_completed_input_is_closed_before_crash_and_stays_closed_after_restart(
    app, monkeypatch, kind
):
    method, actor, update_id, text = final_input(app, kind)
    original = getattr(app.store, method)

    def crash_after_commit(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("simulated stop after mutation commit")

    monkeypatch.setattr(app.store, method, crash_after_commit)
    update = {"update_id": update_id, "message": message(actor, actor, text=text)}
    with pytest.raises(RuntimeError, match="after mutation commit"):
        app.bot.handle(update)
    assert app.store.processed(f"message:{update_id}")
    assert app.store.offset() is None
    reopened = Store(app.store.db.execute("PRAGMA database_list").fetchone()[2])
    bot = Bot(app.bot.settings, reopened, FakeApi(), "tracker_bot", app.bot.now)
    try:
        assert reopened.dialog(actor, actor, None, bot.now()) is None
        bot.handle(update)
        bot.handle(
            {"update_id": update_id + 1, "message": message(actor, actor, text=text)}
        )
        assert reopened.dialog(actor, actor, None, bot.now()) is None
        assert not reopened.processed(f"message:{update_id + 1}")
        if kind == "gift_description":
            assert len(reopened.gifts(101)) == 1
            assert reopened.gifts(101)[0]["description"] == text
        elif kind == "message":
            assert reopened.snapshot(101, bot.now()).message == text
    finally:
        reopened.close()


@pytest.mark.parametrize("kind", ["gift_description", "message", "date", "holiday"])
def test_final_mutation_marker_and_dialog_deletion_roll_back_together(
    app, monkeypatch, kind
):
    _, actor, update_id, text = final_input(app, kind)
    before = app.store.snapshot(101, app.bot.now())
    original = app.store.clear_dialog

    def crash_after_delete(*args):
        original(*args)
        raise RuntimeError("simulated stop after dialog deletion")

    monkeypatch.setattr(app.store, "clear_dialog", crash_after_delete)
    update = {"update_id": update_id, "message": message(actor, actor, text=text)}
    with pytest.raises(RuntimeError, match="after dialog deletion"):
        app.bot.handle(update)
    assert not app.store.processed(f"message:{update_id}")
    assert app.store.dialog(actor, actor, None, app.bot.now())["kind"] == kind
    assert app.store.snapshot(101, app.bot.now()) == before
    assert not app.store.gifts(101)
    assert not app.store.holidays(101)
    monkeypatch.setattr(app.store, "clear_dialog", original)
    app.bot.handle(update)
    assert app.store.processed(f"message:{update_id}")
    assert app.store.dialog(actor, actor, None, app.bot.now()) is None


def fail_ack(api, monkeypatch, description=EXPIRED):
    original = api.call

    def call(method, fields=None, **kwargs):
        if method == "answerCallbackQuery":
            raise ApiError(400, method=method, description=description)
        return original(method, fields, **kwargs)

    monkeypatch.setattr(api, "call", call)


def test_expired_telegram_ack_does_not_drop_valid_local_reset(app, monkeypatch):
    start(app.store)
    cb(app, "sb:reset:101")
    confirmation = last_fields(app)["reply_markup"]["inline_keyboard"][0][0][
        "callback_data"
    ]
    fail_ack(app.api, monkeypatch)
    cb(app, confirmation, update_id=2)
    assert app.store.snapshot(101, app.bot.now()).days == 0
    assert len(app.store.pending()) == 1
    cb(app, confirmation, update_id=2)
    assert len(app.store.pending()) == 1


def test_expired_ack_does_not_bypass_expired_confirmation_or_group_access(
    app, monkeypatch
):
    start(app.store)
    cb(app, "sb:reset:101")
    confirmation = last_fields(app)["reply_markup"]["inline_keyboard"][0][0][
        "callback_data"
    ]
    before = app.store.snapshot(101, app.bot.now()).segment_id
    fail_ack(app.api, monkeypatch)
    cb(app, confirmation, actor=303, chat=-100, thread=7, update_id=2)
    now = app.bot.now()
    app.bot.now = lambda: now + timedelta(minutes=6)
    cb(app, confirmation, update_id=3)
    assert app.store.snapshot(101, app.bot.now()).segment_id == before
    assert not app.store.pending()


def test_unrelated_ack_error_remains_an_error(app, monkeypatch):
    start(app.store)
    fail_ack(app.api, monkeypatch, "Bad Request: unrelated failure")
    with pytest.raises(ApiError):
        cb(app, "sb:reset:101")
    assert not app.store.pending()


@pytest.mark.parametrize("failure", [NetworkError(), ApiError(429, 120), ApiError(500)])
def test_outbox_retry_persists_and_is_not_retried_early(app, failure):
    start(app.store)
    reset(app.store, app.bot.now(), notifications=app.bot.notifications)
    app.api.failure = failure
    app.bot.flush()
    assert len(app.store.pending()) == 1
    attempts = len(app.api.calls)
    app.bot.flush()
    assert len(app.api.calls) == attempts
    reopened = Store(app.store.db.execute("PRAGMA database_list").fetchone()[2])
    try:
        assert not reopened.pending(app.bot.now())
        assert len(reopened.pending()) == 1
    finally:
        reopened.close()
    now = app.bot.now()
    app.bot.now = lambda: now + timedelta(seconds=121)
    app.api.failure = None
    app.bot.flush()
    assert not app.store.pending()


@pytest.mark.parametrize("failure", [NetworkError(), ApiError(429, 120), ApiError(500)])
def test_failed_outbox_does_not_prevent_polling_or_new_command(
    app, monkeypatch, failure
):
    import sobriety.__main__ as runner

    start(app.store)
    reset(app.store, app.bot.now(), notifications=app.bot.notifications)
    signals, calls = {}, []

    def call(method, fields=None, **kwargs):
        calls.append((method, fields))
        if method == "getMe":
            return {"username": "tracker_bot"}
        if method == "getWebhookInfo":
            return {"url": ""}
        if method == "sendMessage" and fields["chat_id"] < 0:
            raise failure
        if method == "getUpdates":
            if sum(name == "getUpdates" for name, _ in calls) == 1:
                return [{"update_id": 50, "message": message(text="/sober")}]
            signals[runner.signal.SIGTERM](None, None)
            return []
        return {"message_id": 999}

    api = SimpleNamespace(call=call)
    app.bot.api = api
    monkeypatch.setattr(runner.Settings, "from_env", lambda: app.bot.settings)
    monkeypatch.setattr(runner, "Store", lambda path: app.store)
    monkeypatch.setattr(runner, "Bot", lambda *args: app.bot)
    monkeypatch.setattr(runner, "Telegram", lambda token: api)
    monkeypatch.setattr(runner, "configure_logging", lambda settings: None)
    monkeypatch.setattr(
        runner.signal, "signal", lambda sig, fn: signals.update({sig: fn})
    )
    # Bound the original regression too, rather than hanging on its retry loop.
    monkeypatch.setattr(
        runner.time, "sleep", lambda _: signals[runner.signal.SIGTERM](None, None)
    )
    monkeypatch.setattr(app.store, "close", lambda: None)
    runner.main()
    assert sum(name == "getUpdates" for name, _ in calls) == 2
    assert any(
        name == "sendMessage" and fields["chat_id"] == 101 for name, fields in calls
    )
    assert app.store.offset() == 51
    assert len(app.store.pending()) == 1


def test_group_gift_clicks_create_no_dialogs_or_force_reply_and_are_actor_bound(app):
    cb(app, "sb:promise:101", actor=303, chat=-100, thread=7, update_id=1)
    first = last_fields(app, "answerCallbackQuery")["url"].split("gift_")[1]
    cb(app, "sb:promise:101", actor=404, chat=-100, thread=7, update_id=2)
    second = last_fields(app, "answerCallbackQuery")["url"].split("gift_")[1]
    assert first != second
    assert not app.store.db.execute("SELECT 1 FROM dialogs").fetchone()
    assert not [fields for method, fields in app.api.calls if method == "sendMessage"]
    app.bot.handle(
        {"update_id": 3, "message": message(404, 404, text=f"/start gift_{first}")}
    )
    assert "не ваша" in last_fields(app)["text"]
    assert not app.store.dialog(404, 404, None, app.bot.now())
    app.bot.handle(
        {"update_id": 4, "message": message(404, 404, text=f"/start gift_{second}")}
    )
    assert app.store.dialog(404, 404, None, app.bot.now())["kind"] == "gift_days"
    assert not app.store.dialog(303, 303, None, app.bot.now())
    assert last_fields(app)["chat_id"] == 404


def test_group_messages_and_cancel_button_cannot_mutate_gifts(app):
    gift = app.store.add_gift(101, 303, "Donor", 8, "Книга", app.bot.now(), "gift")
    cb(app, f"sb:cancelgift:101:{gift}", actor=303, chat=-100, thread=7)
    app.bot.handle({"update_id": 2, "message": message(303, -100, 7, "8")})
    assert not app.store.gifts(101)[0]["cancelled_at"]
    assert not app.store.db.execute("SELECT 1 FROM dialogs WHERE chat<0").fetchone()


def test_private_gift_ttl_and_legacy_group_dialog_cleanup(app):
    open_gift_private(app)
    assert "5 минут" in last_fields(app)["text"]
    now = app.bot.now()
    app.store.tick(now + timedelta(minutes=5))
    assert not app.store.db.execute("SELECT 1 FROM dialogs").fetchone()
    assert app.store.gift_access(303, now + timedelta(minutes=5)) is None
    app.store.db.execute(
        "INSERT INTO dialogs VALUES (303,-100,7,'gift_days',?,50,?)",
        (json.dumps({"uid": 101}), now.timestamp() + 500),
    )
    reopened = Store(app.store.db.execute("PRAGMA database_list").fetchone()[2])
    try:
        assert not reopened.db.execute("SELECT 1 FROM dialogs WHERE chat<0").fetchone()
    finally:
        reopened.close()


def test_private_gift_access_is_revoked_when_origin_binding_is_removed(app):
    from dataclasses import replace

    open_gift_private(app)
    app.bot.settings = replace(app.bot.settings, bindings=())
    app.bot.handle({"update_id": 3, "message": message(303, 303, text="8")})
    assert not app.store.gifts(101)
    assert app.store.dialog(303, 303, None, app.bot.now())["kind"] == "gift_days"
