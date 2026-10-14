"""Operational diagnostics must explain failures without leaking private inputs."""

import io
import json
import logging
import urllib.error
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from sobriety.api import ApiError, NetworkError, Telegram
from sobriety.clock import UTC
from sobriety.diagnostics import DailyLogs, PrivateFormatter, event_action, operation


@pytest.mark.parametrize("http_error", [False, True])
def test_api_error_logs_method_action_description_and_masks_secrets(caplog, http_error):
    token = "123:private_token"
    text = "private user message"
    response = {
        "ok": False,
        "error_code": 400,
        "description": f"Bad Request: message is not modified {token} {text}\nhttps://example.test/private",
    }
    api = Telegram(token)

    def respond(*args, **kwargs):
        body = io.BytesIO(json.dumps(response).encode())
        if http_error:
            raise urllib.error.HTTPError("https://example.test", 400, "bad", {}, body)
        return body

    api._opener = SimpleNamespace(open=respond)
    with caplog.at_level(logging.INFO, logger="sobriety"):
        with pytest.raises(ApiError) as error, operation("button:status"):
            api.call("editMessageText", {"text": text})
    assert error.value.code == 400
    assert error.value.method == "editMessageText"
    assert "message is not modified" in error.value.description
    assert "method=editMessageText action=button:status code=400" in caplog.text
    for private in (token, text, "https://example.test/private"):
        assert private not in caplog.text
        assert private not in error.value.description


def test_network_failure_has_method_without_raw_exception(caplog):
    api = Telegram("123:private_token")

    def broken(*args, **kwargs):
        raise OSError("https://api.telegram.org/bot123:private_token/getUpdates")

    api._opener = SimpleNamespace(open=broken)
    with caplog.at_level(logging.INFO, logger="sobriety"), pytest.raises(NetworkError):
        api.call("getUpdates")
    assert "method=getUpdates" in caplog.text
    assert "private_token" not in caplog.text


@pytest.mark.parametrize(
    "code,method,description,expected",
    [
        (400, "editMessageText", "Bad Request: message is not modified", True),
        (
            400,
            "editMessageText",
            "Bad Request: message is not modified: same content",
            True,
        ),
        (400, "sendMessage", "Bad Request: message is not modified", False),
        (403, "editMessageText", "Bad Request: message is not modified", False),
        (400, "editMessageText", "Bad Request: message to edit not found", False),
        (400, "editMessageText", None, False),
    ],
)
def test_only_exact_unchanged_edit_is_classified_as_expected(
    code, method, description, expected
):
    assert (
        ApiError(code, method=method, description=description).message_not_modified
        is expected
    )


def test_action_names_do_not_echo_users_or_arbitrary_callback_data():
    assert (
        event_action({"message": {"text": "/sober@ExampleBot @private_user"}})
        == "command:sober"
    )
    assert (
        event_action({"callback_query": {"data": "sb:confirm:111:private_nonce"}})
        == "button:confirm"
    )
    assert (
        event_action({"callback_query": {"data": "sb:private_text:111"}})
        == "button:unknown"
    )
    assert event_action({"message": {"text": "private message"}}) == "message:input"


def test_daily_logs_keep_only_three_calendar_days_after_rotation_and_restart(tmp_path):
    now = [datetime(2026, 10, 10, 23, 59, tzinfo=UTC)]
    unrelated = tmp_path / "sobriety.sqlite3"
    unrelated.write_text("keep database")
    handler = DailyLogs(tmp_path, now=lambda: now[0])
    record = logging.LogRecord("sobriety", logging.INFO, __file__, 1, "entry", (), None)
    for _ in range(5):
        handler.handle(record)
        now[0] += timedelta(days=1)
    handler.close()
    assert sorted(path.name for path in tmp_path.glob("*.log")) == [
        "sobriety-2026-10-12.log",
        "sobriety-2026-10-13.log",
        "sobriety-2026-10-14.log",
    ]
    now[0] += timedelta(days=7)
    handler = DailyLogs(tmp_path, now=lambda: now[0])
    handler.handle(record)
    handler.close()
    assert [path.name for path in tmp_path.glob("*.log")] == ["sobriety-2026-10-22.log"]
    assert unrelated.read_text() == "keep database"
    assert (tmp_path / "sobriety-2026-10-22.log").stat().st_mode & 0o777 == 0o600


def test_same_day_restart_appends_and_formatter_redacts_traceback(tmp_path):
    def now():
        return datetime(2026, 10, 14, tzinfo=UTC)

    for label in ("first", "second"):
        handler = DailyLogs(tmp_path, now=now)
        handler.setFormatter(PrivateFormatter("123:private_token"))
        try:
            raise RuntimeError(
                "token=123:private_token url=https://example.test/private"
            )
        except RuntimeError:
            import sys

            record = logging.LogRecord(
                "sobriety", logging.ERROR, __file__, 1, label, (), sys.exc_info()
            )
        record.created = 0
        handler.handle(record)
        handler.close()
    content = (tmp_path / "sobriety-2026-10-14.log").read_text()
    assert "first" in content and "second" in content and "Traceback" in content
    assert "1970-01-01 00:00:00" in content
    assert "private_token" not in content
    assert "https://example.test/private" not in content
