"""Action-aware diagnostics with private files for three UTC calendar days."""

import logging
import re
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date, datetime, timedelta
from pathlib import Path

from .clock import UTC

action = ContextVar("sobriety_action", default="startup")
log = logging.getLogger("sobriety")
CALLBACKS = {
    "status",
    "collection",
    "gifts",
    "date",
    "message",
    "holiday",
    "holidays",
    "unholiday",
    "reset",
    "confirm",
    "received",
    "promise",
    "cancelgift",
}


def redact(text, secrets=()):
    text = str(text)
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[secret]")
    text = re.sub(r"https?://[^\s<>\"]+", "[url]", text)
    text = re.sub(r"\b\d+:[A-Za-z0-9_-]{20,}", "[token]", text)
    return text


def event_action(update):
    callback = update.get("callback_query")
    if isinstance(callback, dict):
        parts = str(callback.get("data", "")).split(":")
        if len(parts) in (3, 4) and parts[0] == "sb" and parts[1] in CALLBACKS:
            return "button:" + parts[1]
        return "button:unknown"
    message = update.get("message")
    if isinstance(message, dict):
        text = message.get("text")
        if isinstance(text, str) and text.startswith("/"):
            command = text.split(maxsplit=1)[0].partition("@")[0]
            return "command:" + (
                command[1:] if command in {"/start", "/sober"} else "other"
            )
        return "message:input"
    return "event:unsupported"


@contextmanager
def operation(name):
    token = action.set(name)
    log.info("Действие %s: начало", name)
    try:
        yield
    except Exception as exc:
        # Values from validation exceptions may contain input: log the type only.
        log.error("Действие %s: ошибка %s", name, type(exc).__name__)
        raise
    else:
        log.info("Действие %s: завершено", name)
    finally:
        action.reset(token)


class PrivateFormatter(logging.Formatter):
    converter = time.gmtime

    def __init__(self, token):
        super().__init__(
            "%(asctime)s UTC | %(levelname)s | action=%(action)s | %(message)s"
        )
        self.token = token

    def format(self, record):
        record.action = action.get()
        return redact(super().format(record), (self.token,))


class DailyLogs(logging.FileHandler):
    def __init__(self, directory, now=None):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.now = now or (lambda: datetime.now(UTC))
        self.day = self.now().astimezone(UTC).date()
        super().__init__(self.directory / f"sobriety-{self.day}.log", encoding="utf-8")
        Path(self.baseFilename).chmod(0o600)
        self.prune()

    def prune(self):
        cutoff = self.day - timedelta(days=2)
        for path in self.directory.glob("sobriety-????-??-??.log"):
            try:
                day = date.fromisoformat(path.name[9:-4])
            except ValueError:
                continue
            if day < cutoff:
                path.unlink()

    def emit(self, record):
        day = self.now().astimezone(UTC).date()
        if day != self.day:
            self.flush()
            self.stream.close()
            self.day = day
            self.baseFilename = str((self.directory / f"sobriety-{day}.log").resolve())
            self.stream = self._open()
            Path(self.baseFilename).chmod(0o600)
            self.prune()
        super().emit(record)


def configure_logging(settings):
    formatter = PrivateFormatter(settings.token)
    handlers = [logging.StreamHandler(), DailyLogs(settings.db_path.parent / "logs")]
    for handler in list(log.handlers):
        if getattr(handler, "sobriety_owned", False):
            log.removeHandler(handler)
            handler.close()
    for handler in handlers:
        handler.sobriety_owned = True
        handler.setFormatter(formatter)
        log.addHandler(handler)
    log.setLevel(logging.INFO)
