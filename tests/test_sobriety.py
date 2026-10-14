"""Standalone tracker: time boundaries, persistent awards, access and button flows."""

from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

from sobriety.api import ApiError, NetworkError, Telegram
from sobriety.bot import Bot
from sobriety.clock import (
    MILESTONES,
    MOSCOW,
    UTC,
    accounting_date,
    boundary,
    elapsed,
    parse_start,
)
from sobriety.settings import Binding, Settings, Subject
from sobriety.store import Store
from sobriety.views import collection, reset_status, status


@pytest.fixture(autouse=True)
def _no_runtime_log_files(monkeypatch):
    # Startup tests replace the DB, so its default /data path is not writable.
    # File retention and formatting are covered in test_sobriety_diagnostics.py.
    import sobriety.__main__ as runner

    monkeypatch.setattr(runner, "configure_logging", lambda settings: None)


def msk(text):
    return datetime.fromisoformat(text).replace(tzinfo=MOSCOW).astimezone(UTC)


MONDAY = msk("2026-10-12T07:00")


@pytest.mark.parametrize(
    "text,days,hours",
    [
        ("2026-10-12T07:00", 0, 0),
        ("2026-10-13T06:59", 0, 23),
        ("2026-10-13T07:00", 1, 24),
        ("2026-10-14T07:00", 2, 48),
        ("2026-10-14T09:00", 2, 50),
    ],
)
def test_exact_owner_example(text, days, hours):
    assert elapsed(MONDAY, msk(text), set()) == (days, hours)


def test_partial_day_after_reset_is_not_a_full_07_to_07_interval():
    start = msk("2026-10-12T23:00")
    assert elapsed(start, msk("2026-10-13T07:00"), set()) == (0, 8)
    assert elapsed(start, msk("2026-10-14T07:00"), set()) == (1, 32)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("2026-10-13T06:59", date(2026, 10, 12)),
        ("2026-10-13T07:00", date(2026, 10, 13)),
    ],
)
def test_holiday_label_uses_shifted_moscow_day(text, expected):
    assert accounting_date(msk(text)) == expected


def test_holiday_excludes_whole_interval_and_current_partial_hours():
    holidays = {date(2026, 10, 12)}
    assert elapsed(MONDAY, msk("2026-10-12T23:00"), holidays) == (0, 0)
    assert elapsed(MONDAY, msk("2026-10-14T07:00"), holidays) == (1, 24)
    assert elapsed(MONDAY, msk("2026-10-14T07:00"), set()) == (2, 48)


def test_clock_is_independent_of_server_timezone():
    assert boundary(date(2026, 10, 12)).hour == 4
    with pytest.raises(ValueError):
        elapsed(datetime(2026, 10, 12), MONDAY, set())


@pytest.mark.parametrize(
    "left,right,hours",
    [
        ("2010-03-27T07:00", "2010-03-28T07:00", 23),
        ("2014-10-25T07:00", "2014-10-26T07:00", 25),
    ],
)
def test_historical_moscow_offset_changes_still_count_one_accounting_day(
    left, right, hours
):
    start_at, end_at = msk(left), msk(right)
    assert elapsed(start_at, end_at, set()) == (1, hours)
    assert elapsed(start_at, end_at, {accounting_date(start_at)}) == (0, 0)


@pytest.mark.parametrize(
    "bad", ["tomorrow", "31.02.2026", "2027-01-01", "1999-12-31", "2026-10-14"]
)
def test_start_date_rejects_invalid_and_future(bad):
    with pytest.raises(ValueError):
        parse_start(bad, msk("2026-10-14T06:59"))


@pytest.mark.parametrize("text", ["12.10.2026", "2026-10-12"])
def test_start_date_is_seven_am(text):
    assert parse_start(text, MONDAY) == MONDAY


@pytest.fixture
def store(tmp_path):
    result = Store(tmp_path / "tracker.sqlite3")
    result.register((Subject(101, "alice_test"), Subject(202, "bravo_test")))
    yield result
    result.close()


def start(store, uid=101):
    store.set_start(uid, MONDAY, MONDAY, f"start:{uid}")


def reset(store, now, key="reset:1", uid=101, notifications=lambda before, after: []):
    token = store.confirmation(uid, uid, now)
    return store.reset(uid, uid, token, now, key, notifications)


def test_record_is_a_milestone_and_survives_reset_and_restart(store):
    start(store)
    now = MONDAY + timedelta(days=9)
    snapshot = store.snapshot(101, now)
    assert snapshot.days == 9 and snapshot.record == 7
    assert [a["threshold"] for a in snapshot.awards] == [1, 3, 5, 7]
    earned = snapshot.awards
    reset(store, now)
    current = store.snapshot(101, now)
    assert current.days == 0 and current.record == 7
    assert current.title == "ахуеть, оно смогло..."
    reopened = Store(store.db.execute("PRAGMA database_list").fetchone()[2])
    try:
        current = reopened.snapshot(101, now + timedelta(days=3))
        assert current.awards == earned
        current = reopened.snapshot(101, now + timedelta(days=10))
        assert current.record == 10 and current.title == "не верю))"
    finally:
        reopened.close()


def test_reset_catches_up_awards_without_prior_status(store):
    start(store)
    now = MONDAY + timedelta(days=30)
    reset(store, now)
    current = store.snapshot(101, now)
    assert current.record == 30 and len(current.awards) == 8


def test_milestone_date_excludes_holidays(store):
    start(store)
    store.add_holiday(101, "Праздник", MONDAY + timedelta(hours=2), "holiday")
    snapshot = store.snapshot(101, MONDAY + timedelta(days=2))
    assert snapshot.days == 1
    assert snapshot.awards[0]["earned_at"] == (MONDAY + timedelta(days=2)).isoformat()
    assert "14.10.2026" in collection(snapshot)


def test_cancel_holiday_restores_count_once_and_survives_reset(store):
    start(store)
    store.add_holiday(101, "Повод", MONDAY, "holiday")
    store.add_holiday(101, "Повод", MONDAY, "holiday")
    now = MONDAY + timedelta(days=3)
    assert store.snapshot(101, now).days == 2
    reset(store, now)
    store.cancel_holiday(101, "2026-10-12", now, "cancel")
    store.cancel_holiday(101, "2026-10-12", now, "cancel")
    assert store.snapshot(101, now).record == 3
    assert not store.holidays(101)


def test_reset_confirmation_binds_actor_chat_series_and_expiry(store):
    start(store)
    now = MONDAY + timedelta(days=3)
    token = store.confirmation(101, 101, now)
    for uid, chat, moment in [
        (202, 101, now),
        (101, -100, now),
        (101, 101, now + timedelta(minutes=6)),
    ]:
        with pytest.raises(ValueError):
            store.reset(uid, chat, token, moment, f"bad:{uid}:{chat}", lambda b, a: [])
    store.reset(101, 101, token, now, "reset", lambda b, a: [])
    with pytest.raises(ValueError):
        store.reset(101, 101, token, now, "reset_again", lambda b, a: [])
    assert (
        store.db.execute("SELECT COUNT(*) FROM segments WHERE user_id=101").fetchone()[
            0
        ]
        == 2
    )


def test_duplicate_reset_does_not_change_series_or_enqueue_twice(store):
    start(store)
    now = MONDAY + timedelta(days=7)
    token = store.confirmation(101, 101, now)

    def notifications(before, after):
        return [{"text": reset_status(before, after)}]

    first = store.reset(101, 101, token, now, "same_update", notifications)
    second = store.reset(101, 101, token, now, "same_update", notifications)
    assert first == second and first["old_days"] == 7
    assert len(store.pending()) == 1


def test_outbox_and_reset_roll_back_together(store):
    start(store)
    now = MONDAY + timedelta(days=7)
    token = store.confirmation(101, 101, now)

    def broken(before, after):
        yield {"text": "one"}
        raise RuntimeError("failure")

    with pytest.raises(RuntimeError):
        store.reset(101, 101, token, now, "reset", broken)
    assert store.snapshot(101, now).days == 7
    assert not store.pending()


def test_arbitrary_gift_threshold_and_recipient_confirmation(store):
    start(store)
    gift = store.add_gift(101, 303, "Donor", 8, "Книга", MONDAY, "gift")
    assert store.add_gift(101, 303, "Donor", 8, "Книга", MONDAY, "gift") == gift
    store.receive_gift(202, gift, MONDAY + timedelta(days=8), "wrong")
    assert not store.gifts(101)[0]["received_at"]
    store.snapshot(101, MONDAY + timedelta(days=8))
    assert store.gifts(101)[0]["eligible_at"]
    reset(store, MONDAY + timedelta(days=8))
    store.receive_gift(101, gift, MONDAY + timedelta(days=8), "received")
    store.receive_gift(101, gift, MONDAY + timedelta(days=9), "received2")
    assert (
        store.gifts(101)[0]["received_at"] == (MONDAY + timedelta(days=8)).isoformat()
    )


def test_unearned_gift_can_only_be_cancelled_by_its_donor(store):
    start(store)
    gift = store.add_gift(101, 303, "Donor", 8, "Книга", MONDAY, "gift")
    store.cancel_gift(101, 404, gift, MONDAY, "wrong_donor")
    assert not store.gifts(101)[0]["cancelled_at"]
    store.cancel_gift(101, 303, gift, MONDAY, "correct_donor")
    store.snapshot(101, MONDAY + timedelta(days=10))
    assert not store.gifts(101)[0]["eligible_at"]


def test_gift_cannot_be_cancelled_after_threshold_without_prior_refresh(store):
    start(store)
    gift = store.add_gift(101, 303, "Donor", 8, "Книга", MONDAY, "gift")
    store.cancel_gift(101, 303, gift, MONDAY + timedelta(days=8), "cancel")
    current = store.gifts(101)[0]
    assert current["eligible_at"] and not current["cancelled_at"]


def test_users_do_not_share_awards_or_holidays(store):
    start(store)
    start(store, 202)
    store.add_holiday(101, "Праздник", MONDAY, "h")
    now = MONDAY + timedelta(days=1)
    assert store.snapshot(101, now).record == 0
    assert store.snapshot(202, now).record == 1


def test_original_texts_are_exact_and_user_text_is_escaped(store):
    expected = [
        "ДОЛБОЕЕЕЕБ",
        "оно живое",
        "оп оп живем живем",
        "ахуеть, оно смогло...",
        "не верю))",
        "ну все-все, харош",
        "он точно жив?...",
        "НУ ЛЕГЕНДА!",
    ]
    assert list(MILESTONES.values()) == expected
    start(store)
    store.set_message(101, "<b>Мне & вам</b>", "msg")
    now = MONDAY + timedelta(days=9)
    before = store.snapshot(101, now)
    assert (
        status(before)
        == "Статус @alice_test:\n- не пил: 9 день\n- рекорд: 7 дней\n- звание: ахуеть, оно смогло..."
    )
    reset(store, now)
    text = reset_status(before, store.snapshot(101, now))
    assert text.startswith(
        "<b>&lt;b&gt;Мне &amp; вам&lt;/b&gt;</b>\nСтатус @alice_test:"
    )
    assert (
        "- звание: ахуеть, оно смогло... (изменено на) -> ахуеть, оно смогло..." in text
    )
    assert text.endswith("Было: 9 дней → стало: 0 дней")


class FakeApi:
    def __init__(self):
        self.calls = []
        self.counter = 50
        self.failure = None

    def call(self, method, fields=None, **kwargs):
        self.calls.append((method, fields))
        if self.failure and method == "sendMessage":
            raise self.failure
        self.counter += 1
        return {"message_id": self.counter}


@pytest.fixture
def app(store):
    settings = Settings(
        "0:fake",
        (Subject(101, "alice_test"), Subject(202, "bravo_test")),
        (Binding(101, -100, 7), Binding(202, -200, 9)),
    )
    api = FakeApi()
    return SimpleNamespace(
        bot=Bot(
            settings, store, api, "tracker_bot", lambda: MONDAY + timedelta(days=9)
        ),
        api=api,
        store=store,
    )


def message(actor=101, chat=101, thread=None, text="/sober", **extra):
    return {
        "message_id": 10,
        "from": {"id": actor, "first_name": "Тест"},
        "chat": {"id": chat, "type": "private" if chat > 0 else "supergroup"},
        "text": text,
        "is_topic_message": thread is not None,
        "message_thread_id": thread,
    } | extra


def cb(app, data, actor=101, chat=101, thread=None, update_id=1):
    app.bot.handle(
        {
            "update_id": update_id,
            "callback_query": {
                "id": str(update_id),
                "from": {"id": actor},
                "data": data,
                "message": message(actor, chat, thread),
            },
        }
    )


def last_fields(app, method="sendMessage"):
    return [fields for name, fields in app.api.calls if name == method][-1]


def open_gift_private(app, actor=303, uid=101, chat=-100, thread=7, update_id=1):
    cb(
        app,
        f"sb:promise:{uid}",
        actor=actor,
        chat=chat,
        thread=thread,
        update_id=update_id,
    )
    token = last_fields(app, "answerCallbackQuery")["url"].split("gift_")[1]
    app.bot.handle(
        {
            "update_id": update_id + 1,
            "message": message(actor, actor, text=f"/start gift_{token}"),
        }
    )
    return token


def test_one_group_command_panel_stays_in_topic_with_original_private_controls(app):
    app.bot.handle({"update_id": 1, "message": message(303, -100, 7)})
    fields = last_fields(app)
    assert fields["chat_id"] == -100 and fields["message_thread_id"] == 7
    labels = [
        b["text"] for row in fields["reply_markup"]["inline_keyboard"] for b in row
    ]
    assert {"Статус", "Коллекция", "Подарки"} <= set(labels)
    assert "Ты долбоеб, давай сначала" not in labels
    cb(app, "sb:collection:101", actor=303, chat=-100, thread=7)
    assert "Коллекция достижений" in last_fields(app, "editMessageText")["text"]
    app.bot.handle({"update_id": 3, "message": message()})
    labels = [
        b["text"]
        for row in last_fields(app)["reply_markup"]["inline_keyboard"]
        for b in row
    ]
    assert "Ты долбоеб, давай сначала" in labels and "Сегодня праздник" in labels


@pytest.mark.parametrize(
    "actor,chat,thread",
    [(303, 303, None), (101, -999, 7), (101, -100, 9), (202, 101, None)],
)
def test_forbidden_context_is_silent_and_cannot_change_state(app, actor, chat, thread):
    app.bot.handle({"update_id": 1, "message": message(actor, chat, thread)})
    assert not app.api.calls
    cb(app, "sb:date:101", actor, chat, thread, 2)
    assert not app.store.snapshot(101, MONDAY).start
    assert not [x for x in app.api.calls if x[0] == "sendMessage"]


def test_manual_other_bot_command_is_ignored(app):
    app.bot.handle({"update_id": 1, "message": message(text="/sober@other_bot")})
    assert not app.api.calls


def test_repeated_status_button_is_success_when_telegram_reports_unchanged(
    app, monkeypatch, caplog
):
    import logging

    original = app.api.call

    def call(method, fields=None, **kwargs):
        if method == "editMessageText":
            raise ApiError(
                400,
                method=method,
                description="Bad Request: message is not modified: same content",
            )
        return original(method, fields, **kwargs)

    monkeypatch.setattr(app.api, "call", call)
    with caplog.at_level(logging.INFO, logger="sobriety"):
        cb(app, "sb:status:101")
        cb(app, "sb:status:101", update_id=2)
    assert (
        len([call for call in app.api.calls if call[0] == "answerCallbackQuery"]) == 2
    )
    assert "Панель уже актуальна" in caplog.text
    assert not [
        record for record in caplog.records if record.levelno >= logging.WARNING
    ]
    assert app.store.snapshot(101, MONDAY).start is None


def test_other_edit_error_is_not_silenced(app, monkeypatch):
    original = app.api.call

    def call(method, fields=None, **kwargs):
        if method == "editMessageText":
            raise ApiError(
                400, method=method, description="Bad Request: message to edit not found"
            )
        return original(method, fields, **kwargs)

    monkeypatch.setattr(app.api, "call", call)
    with pytest.raises(ApiError):
        cb(app, "sb:status:101")


@pytest.mark.parametrize(
    "incoming,reason",
    [
        (message(303, -999, 7), "chat_not_allowed"),
        (message(303, -100, 9), "topic_not_allowed"),
        (message(303, 303), "private_not_allowed"),
        (message(text="/sober@private_target_bot"), "command_for_other_bot"),
        (
            message(
                303,
                -100,
                7,
                sender_chat={"id": -100},
                **{"from": {"id": 303, "is_bot": True}},
            ),
            "sender_chat",
        ),
    ],
)
def test_silent_command_rejections_explain_reason_without_ids_or_input(
    app, caplog, incoming, reason
):
    import logging

    with caplog.at_level(logging.INFO, logger="sobriety"):
        app.bot.handle({"update_id": 1, "message": incoming})
    assert f"reason={reason}" in caplog.text
    assert not app.api.calls
    assert "private_target_bot" not in caplog.text
    assert "-999" not in caplog.text


@pytest.mark.parametrize("thread", [None, 19])
def test_second_group_binding_without_topic_allows_same_users_status(app, thread):
    from dataclasses import replace

    app.bot.settings = replace(
        app.bot.settings, bindings=app.bot.settings.bindings + (Binding(101, -300),)
    )
    app.bot.handle(
        {"update_id": 1, "message": message(303, -300, thread, "/sober @alice_test")}
    )
    fields = last_fields(app)
    assert fields["chat_id"] == -300
    assert fields["message_thread_id"] == thread
    assert "@alice_test" in fields["text"]
    labels = [
        button["text"]
        for row in fields["reply_markup"]["inline_keyboard"]
        for button in row
    ]
    assert "Ты долбоеб, давай сначала" not in labels


def test_group_member_cannot_reset_even_with_copied_callback(app):
    start(app.store)
    cb(app, "sb:reset:101", actor=303, chat=-100, thread=7)
    assert app.store.snapshot(101, app.bot.now()).days == 9
    assert last_fields(app, "answerCallbackQuery")["show_alert"] is True


def test_confirmation_can_be_cancelled_then_old_button_is_invalid(app):
    start(app.store)
    cb(app, "sb:reset:101")
    confirm = last_fields(app)["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    cb(app, "sb:status:101", update_id=2)
    cb(app, confirm, update_id=3)
    assert app.store.snapshot(101, app.bot.now()).days == 9
    assert "устарело" in last_fields(app)["text"]


def test_reset_delivery_survives_send_failure_and_restart(app):
    start(app.store)
    cb(app, "sb:reset:101")
    confirm = last_fields(app)["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    cb(app, confirm, update_id=2)
    assert app.store.snapshot(101, app.bot.now()).days == 0
    app.api.failure = NetworkError()
    app.bot.flush()
    assert len(app.store.pending()) == 1
    app.api.failure = None
    current = app.bot.now()
    app.bot.now = lambda: current + timedelta(seconds=3)
    app.bot.flush()
    app.bot.flush()
    assert not app.store.pending()
    assert last_fields(app)["message_thread_id"] == 7
    assert "Было: 9 дней → стало: 0 дней" in last_fields(app)["text"]


def test_removed_subject_binding_cannot_deliver_old_outbox(app):
    start(app.store)
    reset(app.store, app.bot.now(), notifications=app.bot.notifications)
    app.bot.settings = Settings(
        "0:fake", (Subject(202, "bravo_test"),), (Binding(202, -100, 7),)
    )
    app.bot.flush()
    assert not app.api.calls
    assert not app.store.pending()


def test_terminal_outbox_failure_does_not_block_all_updates(app):
    start(app.store)
    reset(app.store, app.bot.now(), notifications=app.bot.notifications)
    app.api.failure = ApiError(403)
    app.bot.flush()
    assert not app.store.pending()


def test_private_gift_wizard_checks_actor_and_reply_id(app):
    open_gift_private(app)
    prompt = app.api.counter
    for actor, chat, reply in [
        (404, 404, prompt),
        (303, -100, prompt),
        (303, 303, prompt + 1),
    ]:
        app.bot.handle(
            {
                "update_id": 3,
                "message": message(
                    actor, chat, text="8", reply_to_message={"message_id": reply}
                ),
            }
        )
    assert not app.store.gifts(101)
    app.bot.handle(
        {
            "update_id": 4,
            "message": message(
                303, 303, text="8", reply_to_message={"message_id": prompt}
            ),
        }
    )
    app.bot.handle(
        {
            "update_id": 5,
            "message": message(
                303,
                303,
                text="<Книга>",
                reply_to_message={"message_id": app.api.counter},
            ),
        }
    )
    assert app.store.gifts(101)[0]["threshold"] == 8
    assert "&lt;Книга&gt;" in last_fields(app)["text"]


def test_private_date_and_message_flows_persist_exact_input(app):
    cb(app, "sb:date:101")
    app.bot.handle({"update_id": 2, "message": message(text="12.10.2026")})
    assert app.store.snapshot(101, app.bot.now()).days == 9
    cb(app, "sb:message:101", update_id=3)
    app.bot.handle({"update_id": 4, "message": message(text="  Мой текст!  ")})
    assert app.store.snapshot(101, app.bot.now()).message == "  Мой текст!  "
    assert last_fields(app)["text"].startswith("Статус @alice_test:")


def test_saved_message_is_bold_and_escaped_only_in_group_reset(app):
    draft = '<a href="https://example.test">Мой & текст</a>'
    app.store.set_message(101, draft, "draft")
    app.bot.handle({"update_id": 1, "message": message()})
    assert "example.test" not in last_fields(app)["text"]
    cb(app, "sb:status:101", update_id=2)
    assert "example.test" not in last_fields(app, "editMessageText")["text"]
    app.bot.handle({"update_id": 3, "message": message(303, -100, 7)})
    assert last_fields(app)["text"].startswith("Статус @alice_test:")
    assert "example.test" not in last_fields(app)["text"]
    assert app.store.snapshot(101, app.bot.now()).message == draft
    start(app.store)
    reset(app.store, app.bot.now(), notifications=app.bot.notifications)
    app.bot.flush()
    expected = "<b>&lt;a href=&quot;https://example.test&quot;&gt;Мой &amp; текст&lt;/a&gt;</b>\nСтатус @alice_test:"
    assert last_fields(app)["chat_id"] == -100
    assert last_fields(app)["text"].startswith(expected)


def test_confirmed_reset_allows_one_new_date_in_private_and_preserves_awards(app):
    cb(app, "sb:date:101", update_id=1)
    app.bot.handle({"update_id": 2, "message": message(text="12.10.2026")})
    before = app.store.snapshot(101, app.bot.now())
    cb(app, "sb:reset:101", update_id=3)
    confirm = last_fields(app)["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    cb(app, confirm, update_id=4)
    assert app.store.snapshot(101, app.bot.now()).start == app.bot.now()
    cb(app, "sb:date:101", update_id=5)
    assert "ДД.ММ.ГГГГ" in last_fields(app)["text"]
    app.bot.handle({"update_id": 6, "message": message(text="20.10.2026")})
    after = app.store.snapshot(101, app.bot.now())
    assert after.start == msk("2026-10-20T07:00")
    assert after.days == 1
    assert (after.record, after.title, after.awards) == (
        before.record,
        before.title,
        before.awards,
    )
    segments = app.store.db.execute(
        "SELECT start,end FROM segments WHERE user_id=101 ORDER BY id"
    ).fetchall()
    assert len(segments) == 2
    assert segments[0]["start"] == before.start.isoformat()
    assert segments[0]["end"] == app.bot.now().isoformat()
    assert segments[1]["end"] is None
    cb(app, "sb:date:101", update_id=7)
    assert (
        last_fields(app)["text"]
        == "Дата уже задана. Новую серию начните кнопкой сброса."
    )


def test_existing_automatic_reset_can_be_dated_after_restart_without_migration(store):
    start(store)
    now = MONDAY + timedelta(days=9)
    reset(store, now)
    # Previous versions did not write this metadata; use their actual shape.
    store.db.execute("DELETE FROM meta WHERE key LIKE 'start_locked:%'")
    path = store.db.execute("PRAGMA database_list").fetchone()[2]
    reopened = Store(path)
    try:
        assert reopened.can_set_start(101)
        assert reopened.db.execute("PRAGMA user_version").fetchone()[0] == 1
        reopened.set_start(101, now - timedelta(days=1), now, "date-after-upgrade")
        assert not reopened.can_set_start(101)
        assert reopened.snapshot(101, now).record == 7
    finally:
        reopened.close()


def test_reentering_same_reset_date_is_locked_even_after_restart(store):
    start(store)
    now = MONDAY + timedelta(days=1)
    reset(store, now)
    store.set_start(101, now, now, "choose-same-date")
    assert not store.can_set_start(101)
    path = store.db.execute("PRAGMA database_list").fetchone()[2]
    reopened = Store(path)
    try:
        assert not reopened.can_set_start(101)
        with pytest.raises(ValueError, match="Дата уже задана"):
            reopened.set_start(101, MONDAY, now, "another-date")
    finally:
        reopened.close()


def test_every_reset_unlocks_date_and_replayed_input_cannot_mutate_next_series(store):
    start(store)
    now = MONDAY + timedelta(days=1)
    reset(store, now)
    store.set_start(101, MONDAY, now, "chosen-date")
    assert not store.can_set_start(101)
    reset(store, now + timedelta(hours=1), key="reset:2")
    segment = store.snapshot(101, now + timedelta(hours=1))
    assert store.can_set_start(101)
    store.set_start(101, MONDAY, now + timedelta(hours=1), "chosen-date")
    assert store.can_set_start(101)
    assert store.snapshot(101, now + timedelta(hours=1)).start == segment.start


def test_new_date_invalidates_confirmation_for_the_previous_start(store):
    start(store)
    now = MONDAY + timedelta(days=1)
    reset(store, now)
    token = store.confirmation(101, 101, now)
    store.set_start(101, MONDAY, now, "new-date")
    with pytest.raises(ValueError, match="Подтверждение устарело"):
        store.reset(101, 101, token, now, "stale-confirm", lambda b, a: [])
    assert store.snapshot(101, now).start == MONDAY


def test_invalid_date_after_reset_keeps_date_input_available(app):
    start(app.store)
    reset(app.store, app.bot.now())
    cb(app, "sb:date:101", update_id=1)
    app.bot.handle({"update_id": 2, "message": message(text="31.02.2026")})
    assert app.store.can_set_start(101)
    app.bot.handle({"update_id": 3, "message": message(text="20.10.2026")})
    assert app.store.snapshot(101, app.bot.now()).days == 1
    assert not app.store.can_set_start(101)


def test_config_validates_bindings_without_printing_secret(monkeypatch):
    monkeypatch.setenv("SOBRIETY_BOT_TOKEN", "0:secret_token")
    monkeypatch.setenv("SOBRIETY_BOT_USERNAME", "tracker_bot")
    monkeypatch.setenv("SOBRIETY_USERS", '[{"user_id":101,"username":"alice_test"}]')
    monkeypatch.setenv(
        "SOBRIETY_BINDINGS", '[{"user_id":101,"chat_id":-100,"thread_id":7}]'
    )
    settings = Settings.from_env()
    assert settings.subjects_for(-100, 7) == {101}
    assert settings.subjects_for(-100, 9) == set()
    monkeypatch.setenv("SOBRIETY_BINDINGS", '[{"user_id":202,"chat_id":-100}]')
    with pytest.raises(SystemExit) as err:
        Settings.from_env()
    assert "secret_token" not in str(err.value)


def test_api_network_error_never_exposes_token():
    api = Telegram("0:secret_token")

    def broken(*args, **kwargs):
        raise OSError("url contained secret_token")

    api._opener = SimpleNamespace(open=broken)
    with pytest.raises(NetworkError) as err:
        api.call("getMe")
    assert "secret_token" not in str(err.value)


def test_cancel_holiday_restores_gift_earned_in_closed_series(store):
    start(store)
    gift = store.add_gift(101, 303, "Donor", 3, "Книга", MONDAY, "gift")
    store.add_holiday(101, "Ошибка", MONDAY, "holiday")
    now = MONDAY + timedelta(days=3)
    reset(store, now)
    assert not store.gifts(101)[0]["eligible_at"]
    store.cancel_holiday(101, "2026-10-12", now, "cancel")
    assert store.gifts(101)[0]["id"] == gift
    assert store.gifts(101)[0]["eligible_at"]


def test_cancel_old_holiday_does_not_unlock_a_new_promise(store):
    start(store)
    store.add_holiday(101, "Ошибка", MONDAY, "holiday")
    now = MONDAY + timedelta(days=3)
    reset(store, now)
    store.add_gift(101, 303, "Donor", 3, "Книга", now + timedelta(hours=1), "gift")
    store.cancel_holiday(101, "2026-10-12", now + timedelta(hours=2), "cancel")
    assert not store.gifts(101)[0]["eligible_at"]


def test_promise_added_after_reset_does_not_unlock_from_lifetime_record(store):
    start(store)
    now = MONDAY + timedelta(days=10)
    reset(store, now)
    store.add_gift(101, 303, "Donor", 7, "Книга", now, "gift")
    assert not store.gifts(101)[0]["eligible_at"]
    store.snapshot(101, now + timedelta(days=7))
    assert store.gifts(101)[0]["eligible_at"]


def test_dialog_expires_and_persists_in_sqlite(store):
    store.put_dialog(303, 303, None, "gift_days", {"uid": 101}, 50, MONDAY)
    reopened = Store(store.db.execute("PRAGMA database_list").fetchone()[2])
    try:
        assert reopened.dialog(303, 303, None, MONDAY)["prompt"] == 50
        assert reopened.dialog(303, 303, None, MONDAY + timedelta(minutes=5)) is None
    finally:
        reopened.close()


def test_private_input_error_keeps_gift_wizard_at_same_step(app):
    open_gift_private(app)
    old_prompt = app.api.counter
    app.bot.handle(
        {
            "update_id": 3,
            "message": message(
                303, 303, text="0", reply_to_message={"message_id": old_prompt}
            ),
        }
    )
    assert app.store.dialog(303, 303, None, app.bot.now())["kind"] == "gift_days"
    assert "Введите число дней" in last_fields(app)["text"]
    app.bot.handle(
        {
            "update_id": 4,
            "message": message(
                303, 303, text="11", reply_to_message={"message_id": old_prompt}
            ),
        }
    )
    app.bot.handle(
        {
            "update_id": 5,
            "message": message(
                303,
                303,
                text="Подарок",
                reply_to_message={"message_id": app.api.counter},
            ),
        }
    )
    assert app.store.gifts(101)[0]["threshold"] == 11


def test_group_selects_target_by_username_reply_and_buttons(app):
    app.bot.settings = Settings(
        "0:fake",
        app.bot.settings.subjects,
        (Binding(101, -100, 7), Binding(202, -100, 7)),
    )
    app.bot.handle({"update_id": 1, "message": message(303, -100, 7)})
    assert last_fields(app)["text"] == "Выберите пользователя:"
    app.bot.handle(
        {"update_id": 2, "message": message(303, -100, 7, "/sober @bravo_test")}
    )
    assert "@bravo_test" in last_fields(app)["text"]
    app.bot.handle(
        {
            "update_id": 3,
            "message": message(303, -100, 7, reply_to_message={"from": {"id": 101}}),
        }
    )
    assert "@alice_test" in last_fields(app)["text"]
    cb(app, "sb:collection:202", actor=303, chat=-100, thread=7, update_id=4)
    assert "@bravo_test" in last_fields(app, "editMessageText")["text"]


@pytest.mark.parametrize(
    "data",
    [
        "",
        "other:status:101",
        "sb:reset:nope",
        "sb:status:999",
        "sb:reset:101:extra:extra",
    ],
)
def test_invalid_callbacks_cannot_mutate(app, data):
    start(app.store)
    cb(app, data)
    assert app.store.snapshot(101, app.bot.now()).days == 9


def test_private_holiday_can_be_cancelled_from_buttons(app):
    start(app.store)
    cb(app, "sb:holiday:101")
    app.bot.handle({"update_id": 2, "message": message(text="Праздник")})
    assert app.store.holidays(101)[0]["name"] == "Праздник"
    cb(app, "sb:holidays:101", update_id=3)
    callback = last_fields(app)["reply_markup"]["inline_keyboard"][0][0][
        "callback_data"
    ]
    cb(app, callback, update_id=4)
    assert not app.store.holidays(101)


def test_database_offset_survives_restart(store):
    store.advance(123)
    reopened = Store(store.db.execute("PRAGMA database_list").fetchone()[2])
    try:
        assert reopened.offset() == 124
    finally:
        reopened.close()


@pytest.mark.parametrize(
    "raw",
    [
        '[{"user_id":true,"username":"alice_test"}]',
        '[{"user_id":101,"username":"alice_test"},{"user_id":101,"username":"other_name"}]',
        '[{"user_id":101,"username":"invalid/name"}]',
        "[]",
        "{}",
    ],
)
def test_bad_subject_configuration_fails_closed(monkeypatch, raw):
    monkeypatch.setenv("SOBRIETY_BOT_TOKEN", "0:secret")
    monkeypatch.setenv("SOBRIETY_BOT_USERNAME", "tracker_bot")
    monkeypatch.setenv("SOBRIETY_USERS", raw)
    monkeypatch.setenv("SOBRIETY_BINDINGS", '[{"user_id":101,"chat_id":-100}]')
    with pytest.raises(SystemExit):
        Settings.from_env()


def test_api_error_does_not_echo_non_numeric_response_code():
    assert "secret" not in str(ApiError("secret", -1))
    assert ApiError(429, -1).retry_after is None


def test_startup_refuses_existing_webhook_without_removing_it(app, monkeypatch):
    import sobriety.__main__ as runner

    calls = []

    def call(method, *args, **kwargs):
        calls.append(method)
        return (
            {"username": "tracker_bot"} if method == "getMe" else {"url": "configured"}
        )

    monkeypatch.setattr(runner.Settings, "from_env", lambda: app.bot.settings)
    monkeypatch.setattr(runner, "Store", lambda path: app.store)
    monkeypatch.setattr(runner, "Telegram", lambda token: SimpleNamespace(call=call))
    monkeypatch.setattr(runner.signal, "signal", lambda *args: None)
    with pytest.raises(SystemExit):
        runner.main()
    assert calls == ["getMe", "getWebhookInfo"]


def test_conflict_stops_polling_without_restart_loop(app, monkeypatch):
    import sobriety.__main__ as runner

    calls, signals = [], {}

    def call(method, *args, **kwargs):
        calls.append(method)
        if method == "getMe":
            return {"username": "tracker_bot"}
        if method == "getWebhookInfo":
            return {"url": ""}
        if method == "getUpdates":
            raise ApiError(409)
        return True

    monkeypatch.setattr(runner.Settings, "from_env", lambda: app.bot.settings)
    monkeypatch.setattr(runner, "Store", lambda path: app.store)
    monkeypatch.setattr(runner, "Telegram", lambda token: SimpleNamespace(call=call))
    monkeypatch.setattr(
        runner.signal, "signal", lambda sig, fn: signals.update({sig: fn})
    )
    monkeypatch.setattr(
        runner.time, "sleep", lambda seconds: signals[runner.signal.SIGTERM](None, None)
    )
    runner.main()
    assert calls.count("getUpdates") == 1


def test_wrong_bot_identity_is_refused_before_profile_changes(app, monkeypatch):
    from dataclasses import replace

    import sobriety.__main__ as runner

    calls = []

    def call(method, *args, **kwargs):
        calls.append(method)
        return {"username": "wrong_bot"}

    monkeypatch.setattr(
        runner.Settings,
        "from_env",
        lambda: replace(app.bot.settings, expected_username="tracker_bot"),
    )
    monkeypatch.setattr(runner, "Store", lambda path: app.store)
    monkeypatch.setattr(runner, "Telegram", lambda token: SimpleNamespace(call=call))
    monkeypatch.setattr(runner.signal, "signal", lambda *args: None)
    with pytest.raises(SystemExit):
        runner.main()
    assert calls == ["getMe"]


def test_removed_subject_is_not_updated_by_tick(store):
    start(store)
    start(store, 202)
    store.tick(MONDAY + timedelta(days=10), {202})
    awards = list(store.db.execute("SELECT user_id FROM awards"))
    assert awards and {row[0] for row in awards} == {202}


def test_old_menu_scopes_are_deleted_before_polling(app, monkeypatch):
    import sobriety.__main__ as runner

    calls, signals = [], {}
    app.store.save_scopes({-999})

    def call(method, fields=None, **kwargs):
        calls.append((method, fields))
        if method == "getMe":
            return {"username": "tracker_bot"}
        if method == "getWebhookInfo":
            return {"url": ""}
        if method == "getUpdates":
            signals[runner.signal.SIGTERM](None, None)
            return []
        return True

    monkeypatch.setattr(runner.Settings, "from_env", lambda: app.bot.settings)
    monkeypatch.setattr(runner, "Store", lambda path: app.store)
    monkeypatch.setattr(runner, "Telegram", lambda token: SimpleNamespace(call=call))
    monkeypatch.setattr(
        runner.signal, "signal", lambda sig, fn: signals.update({sig: fn})
    )
    runner.main()
    assert ("deleteMyCommands", {"scope": {"type": "chat", "chat_id": -999}}) in calls


def test_panel_navigation_cancels_pending_private_gift_input(app):
    open_gift_private(app)
    prompt = app.api.counter
    cb(app, "sb:gifts:101", actor=303, chat=303, update_id=3)
    app.bot.handle(
        {
            "update_id": 4,
            "message": message(
                303, 303, text="8", reply_to_message={"message_id": prompt}
            ),
        }
    )
    assert app.store.dialog(303, 303, None, app.bot.now()) is None
    assert not app.store.gifts(101)


def test_startup_accepts_owner_who_has_not_opened_private_chat(app, monkeypatch):
    import sobriety.__main__ as runner

    signals = {}

    def call(method, fields=None, **kwargs):
        if method == "getMe":
            return {"username": "tracker_bot"}
        if method == "getWebhookInfo":
            return {"url": ""}
        if method == "setMyCommands" and fields["scope"]["chat_id"] > 0:
            raise ApiError(400)
        if method == "getUpdates":
            signals[runner.signal.SIGTERM](None, None)
            return []
        return True

    monkeypatch.setattr(runner.Settings, "from_env", lambda: app.bot.settings)
    monkeypatch.setattr(runner, "Store", lambda path: app.store)
    monkeypatch.setattr(runner, "Telegram", lambda token: SimpleNamespace(call=call))
    monkeypatch.setattr(
        runner.signal, "signal", lambda sig, fn: signals.update({sig: fn})
    )
    runner.main()
