"""downloaders/common.py: user_reason - что из ошибки загрузчика доходит до человека.

Наш текст (лимит размера и времени) показывается как есть; английский текст yt-dlp
пользователю не уходит, он только в логе.
"""

import pytest
from loguru import logger

from downloaders.common import DownloadLimitError, short_error, user_reason

FALLBACK = "площадка не отдала ролик - попробуйте другую ссылку"
YTDLP_TEXT = "ERROR: [youtube] dQw4w9WgXcQ: Sign in to confirm you're not a bot"


@pytest.fixture
def log_lines():
    lines = []
    sink = logger.add(lambda m: lines.append(m.record["message"]), level="INFO")
    yield lines
    logger.remove(sink)


def test_own_limit_error_is_shown_as_is(log_lines):
    e = DownloadLimitError("файл больше 50 МБ")
    assert user_reason(e, FALLBACK, "youtube") == "файл больше 50 МБ"
    assert log_lines == []  # наши ограничения в лог как ошибки площадки не пишутся


def test_limit_error_wrapped_as_cause_is_found():
    try:
        try:
            raise DownloadLimitError("скачивание заняло слишком много времени")
        except DownloadLimitError as inner:
            raise RuntimeError(YTDLP_TEXT) from inner
    except RuntimeError as outer:
        assert (
            user_reason(outer, FALLBACK, "tiktok")
            == "скачивание заняло слишком много времени"
        )


def test_limit_error_as_explicit_cause_only_is_found():
    # raise ... from ... вне except: есть __cause__, а __context__ пуст
    outer = RuntimeError(YTDLP_TEXT)
    outer.__cause__ = DownloadLimitError("файл больше 7 МБ")
    assert outer.__context__ is None
    assert user_reason(outer, FALLBACK, "youtube") == "файл больше 7 МБ"


def test_limit_error_wrapped_as_context_is_found():
    # yt-dlp оборачивает ошибку хука так: raise DownloadError(...) внутри except
    try:
        try:
            raise DownloadLimitError("файл больше 1 МБ")
        except DownloadLimitError:
            raise RuntimeError(YTDLP_TEXT)
    except RuntimeError as outer:
        assert user_reason(outer, FALLBACK, "youtube") == "файл больше 1 МБ"


def test_foreign_error_gets_fallback_and_english_text_never_reaches_user():
    reason = user_reason(RuntimeError(YTDLP_TEXT), FALLBACK, "youtube")
    assert reason == FALLBACK
    assert "Sign in" not in reason and "ERROR" not in reason


def test_foreign_error_text_goes_to_log_without_error_prefix(log_lines):
    user_reason(RuntimeError(YTDLP_TEXT), FALLBACK, "youtube")
    assert log_lines == [
        "youtube download error: [youtube] dQw4w9WgXcQ: Sign in to confirm you're not a bot"
    ]


def test_log_line_is_cut_to_200_chars(log_lines):
    user_reason(RuntimeError("x" * 1000), FALLBACK, "twitter")
    assert log_lines == ["twitter download error: " + "x" * 200]


def test_foreign_error_with_unrelated_cause_gets_fallback():
    try:
        try:
            raise ValueError("не наша")
        except ValueError as inner:
            raise RuntimeError("тоже не наша") from inner
    except RuntimeError as outer:
        assert user_reason(outer, FALLBACK, "instagram") == FALLBACK


def test_braces_in_foreign_error_do_not_break_logging(log_lines):
    assert (
        user_reason(RuntimeError("bad {format} {0}"), FALLBACK, "youtube") == FALLBACK
    )
    assert log_lines == ["youtube download error: bad {format} {0}"]


def test_short_error_prefers_our_limit_text_and_cuts_foreign():
    assert short_error(DownloadLimitError("лимит")) == "лимит"
    assert short_error(RuntimeError("ERROR: abc")) == "abc"
    assert len(short_error(RuntimeError("y" * 500))) == 200
