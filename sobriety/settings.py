"""Strict isolated configuration. Never read Soroka's .env."""

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Subject:
    user_id: int
    username: str


@dataclass(frozen=True)
class Binding:
    user_id: int
    chat_id: int
    thread_id: int | None = None

    def matches(self, chat_id: int, thread_id: int | None) -> bool:
        return self.chat_id == chat_id and (
            self.thread_id is None or self.thread_id == thread_id
        )


@dataclass(frozen=True)
class Settings:
    token: str
    subjects: tuple[Subject, ...]
    bindings: tuple[Binding, ...]
    db_path: Path = Path("/data/sobriety.sqlite3")
    expected_username: str | None = None

    def subjects_for(self, chat_id: int, thread_id: int | None) -> set[int]:
        return {b.user_id for b in self.bindings if b.matches(chat_id, thread_id)}

    @classmethod
    def from_env(cls):
        try:
            token = os.environ["SOBRIETY_BOT_TOKEN"].strip()
            if not re.fullmatch(r"\d+:[A-Za-z0-9_-]+", token):
                raise ValueError
            expected_username = os.environ["SOBRIETY_BOT_USERNAME"].lstrip("@")
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", expected_username):
                raise ValueError
            raw_users = json.loads(os.environ["SOBRIETY_USERS"])
            raw_bindings = json.loads(os.environ["SOBRIETY_BINDINGS"])
            if not isinstance(raw_users, list) or not isinstance(raw_bindings, list):
                raise ValueError
            subjects = []
            for item in raw_users:
                uid, username = item["user_id"], item["username"].lstrip("@")
                if type(uid) is not int or uid <= 0:
                    raise ValueError
                if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", username):
                    raise ValueError
                subjects.append(Subject(uid, username))
            ids = {s.user_id for s in subjects}
            usernames = {s.username.casefold() for s in subjects}
            if not ids or len(ids) != len(subjects) or len(usernames) != len(subjects):
                raise ValueError
            bindings = []
            for item in raw_bindings:
                uid, chat, thread = (
                    item["user_id"],
                    item["chat_id"],
                    item.get("thread_id"),
                )
                if type(uid) is not int or uid not in ids:
                    raise ValueError
                if type(chat) is not int or chat >= 0:
                    raise ValueError
                if thread is not None and (type(thread) is not int or thread <= 0):
                    raise ValueError
                bindings.append(Binding(uid, chat, thread))
            if not bindings or {b.user_id for b in bindings} != ids:
                raise ValueError
            if len(set(bindings)) != len(bindings) or len(subjects) > 50:
                raise ValueError
            return cls(
                token,
                tuple(subjects),
                tuple(bindings),
                Path(os.environ.get("SOBRIETY_DB_PATH", "/data/sobriety.sqlite3")),
                expected_username,
            )
        except (KeyError, ValueError, TypeError, AttributeError):
            raise SystemExit(
                "Некорректная конфигурация SOBRIETY_* (значения скрыты)."
            ) from None
