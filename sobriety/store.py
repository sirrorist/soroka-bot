"""Persistent series, immutable awards, gifts, dialogs and reset notifications."""

import json
import secrets
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from .clock import MILESTONES, UTC, accounting_date, elapsed, milestone_time

DIALOG_TTL = 300


@dataclass(frozen=True)
class Snapshot:
    user_id: int
    username: str
    message: str
    segment_id: int | None
    start: datetime | None
    days: int
    hours: int
    record: int
    title: str
    awards: tuple[dict, ...]


class Store:
    def __init__(self, path: Path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1):
            self.db.close()
            raise ValueError("Unsupported sobriety database version")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY, username TEXT NOT NULL,
                message TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS segments (
                id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
                start TEXT NOT NULL, end TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS one_open_segment
                ON segments(user_id) WHERE end IS NULL;
            CREATE TABLE IF NOT EXISTS holidays (
                user_id INTEGER NOT NULL REFERENCES users(id), day TEXT NOT NULL,
                name TEXT NOT NULL, PRIMARY KEY(user_id, day)
            );
            CREATE TABLE IF NOT EXISTS awards (
                user_id INTEGER NOT NULL REFERENCES users(id), threshold INTEGER NOT NULL,
                earned_at TEXT NOT NULL, PRIMARY KEY(user_id, threshold)
            );
            CREATE TABLE IF NOT EXISTS gifts (
                id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
                donor_id INTEGER NOT NULL, donor_name TEXT NOT NULL,
                threshold INTEGER NOT NULL CHECK(threshold > 0), description TEXT NOT NULL,
                created_at TEXT NOT NULL, eligible_at TEXT, received_at TEXT,
                cancelled_at TEXT
            );
            CREATE TABLE IF NOT EXISTS operations (
                key TEXT PRIMARY KEY, result TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS dialogs (
                actor INTEGER NOT NULL, chat INTEGER NOT NULL, thread INTEGER NOT NULL,
                kind TEXT NOT NULL, payload TEXT NOT NULL, prompt INTEGER NOT NULL,
                expires REAL NOT NULL, PRIMARY KEY(actor, chat, thread)
            );
            CREATE TABLE IF NOT EXISTS confirmations (
                token TEXT PRIMARY KEY, actor INTEGER NOT NULL, chat INTEGER NOT NULL,
                segment INTEGER NOT NULL, expires REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS outbox (
                id INTEGER PRIMARY KEY, payload TEXT NOT NULL, sent INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            PRAGMA user_version=1;
        """)
        # Group interactions now only display information or hand off to private chat.
        self.db.execute("DELETE FROM dialogs WHERE chat<0")

    def close(self):
        self.db.close()

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def register(self, subjects):
        with self.transaction():
            for subject in subjects:
                self.db.execute(
                    "INSERT INTO users(id, username) VALUES (?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET username=excluded.username",
                    (subject.user_id, subject.username),
                )

    def _holidays(self, uid, now):
        days = {
            date.fromisoformat(r[0])
            for r in self.db.execute("SELECT day FROM holidays WHERE user_id=?", (uid,))
        }
        for row in self.db.execute(
            "SELECT payload FROM dialogs WHERE actor=? AND chat=? AND kind='holiday' AND expires>?",
            (uid, uid, now.timestamp()),
        ):
            payload = json.loads(row[0])
            if payload.get("uid") == uid and payload.get("day"):
                days.add(date.fromisoformat(payload["day"]))
        return days

    def _snapshot(self, uid: int, now: datetime) -> Snapshot:
        user = self.db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        if user is None:
            raise ValueError("Unknown subject")
        segment = self.db.execute(
            "SELECT * FROM segments WHERE user_id=? AND end IS NULL", (uid,)
        ).fetchone()
        start = datetime.fromisoformat(segment["start"]) if segment else None
        holidays = self._holidays(uid, now)
        days, hours = elapsed(start, now, holidays) if start else (0, 0)
        if start:
            for threshold in MILESTONES:
                if days >= threshold:
                    self.db.execute(
                        "INSERT OR IGNORE INTO awards VALUES (?, ?, ?)",
                        (
                            uid,
                            threshold,
                            milestone_time(start, threshold, holidays).isoformat(),
                        ),
                    )
            # Eligibility is earned once; a reset does not retract a promised gift.
            self.db.execute(
                "UPDATE gifts SET eligible_at=? WHERE user_id=? AND threshold<=? "
                "AND eligible_at IS NULL AND cancelled_at IS NULL",
                (now.isoformat(), uid, days),
            )
        awards = tuple(
            dict(r)
            for r in self.db.execute(
                "SELECT threshold, earned_at FROM awards WHERE user_id=? ORDER BY threshold",
                (uid,),
            )
        )
        record = max((r["threshold"] for r in awards), default=0)
        return Snapshot(
            uid,
            user["username"],
            user["message"],
            segment["id"] if segment else None,
            start,
            days,
            hours,
            record,
            MILESTONES.get(record, "—"),
            awards,
        )

    def snapshot(self, uid, now):
        with self.transaction():
            return self._snapshot(uid, now)

    def _repeat(self, key):
        row = self.db.execute(
            "SELECT result FROM operations WHERE key=?", (key,)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def processed(self, key):
        return self._repeat(key) is not None

    def _remember(self, key, result):
        self.db.execute(
            "INSERT INTO operations VALUES (?, ?)", (key, json.dumps(result))
        )
        return result

    def _finish_input(self, key, result, dialog):
        """Called inside the mutation transaction, before committing its result."""
        self._remember(key, result)
        if dialog is not None:
            self.clear_dialog(*dialog)

    def can_set_start(self, uid):
        segment = self.db.execute(
            "SELECT id,start FROM segments WHERE user_id=? AND end IS NULL", (uid,)
        ).fetchone()
        if segment is None:
            return True
        locked = self.db.execute(
            "SELECT value FROM meta WHERE key=?", (f"start_locked:{uid}",)
        ).fetchone()
        if locked and locked[0] == str(segment["id"]):
            return False
        # A reset joins the old end and the new automatic start. This also
        # recognizes existing resets from v1 without a schema migration.
        return (
            self.db.execute(
                "SELECT 1 FROM segments WHERE user_id=? AND id<? AND end=? LIMIT 1",
                (uid, segment["id"], segment["start"]),
            ).fetchone()
            is not None
        )

    def set_start(self, uid, start, now, key, *, dialog=None):
        with self.transaction():
            if self._repeat(key) is not None:
                return
            previous = self._snapshot(uid, now)
            if not self.can_set_start(uid):
                raise ValueError("Дата уже задана. Новую серию начните кнопкой сброса.")
            if previous.start is None:
                segment = self.db.execute(
                    "INSERT INTO segments(user_id,start) VALUES (?, ?)",
                    (uid, start.isoformat()),
                ).lastrowid
            else:
                segment = previous.segment_id
                self.db.execute(
                    "UPDATE segments SET start=? WHERE id=?",
                    (start.isoformat(), segment),
                )
            self.db.execute(
                "INSERT OR REPLACE INTO meta VALUES (?, ?)",
                (f"start_locked:{uid}", str(segment)),
            )
            self.db.execute("DELETE FROM confirmations WHERE actor=?", (uid,))
            self._snapshot(uid, now)
            self._finish_input(key, {"done": True}, dialog)

    def set_message(self, uid, text, key, *, dialog=None):
        with self.transaction():
            if self._repeat(key) is None:
                self.db.execute("UPDATE users SET message=? WHERE id=?", (text, uid))
                self._finish_input(key, {"done": True}, dialog)

    def add_holiday(self, uid, name, now, key, day=None, *, dialog=None):
        with self.transaction():
            if self._repeat(key) is not None:
                return
            if not self.db.execute(
                "SELECT 1 FROM segments WHERE user_id=? AND end IS NULL", (uid,)
            ).fetchone():
                raise ValueError("Сначала задайте дату начала.")
            self.db.execute(
                "INSERT INTO holidays VALUES (?, ?, ?) ON CONFLICT(user_id,day) "
                "DO UPDATE SET name=excluded.name",
                (uid, (day or accounting_date(now)).isoformat(), name),
            )
            self._snapshot(uid, now)
            self._finish_input(key, {"done": True}, dialog)

    def holidays(self, uid, limit=10):
        return [
            dict(r)
            for r in self.db.execute(
                "SELECT day,name FROM holidays WHERE user_id=? ORDER BY day DESC LIMIT ?",
                (uid, limit),
            )
        ]

    def cancel_holiday(self, uid, day, now, key):
        with self.transaction():
            if self._repeat(key) is None:
                self.db.execute(
                    "DELETE FROM holidays WHERE user_id=? AND day=?", (uid, day)
                )
                # Cancelled holidays may restore earned milestones in a closed series.
                holidays = self._holidays(uid, now)
                for segment in self.db.execute(
                    "SELECT * FROM segments WHERE user_id=?", (uid,)
                ):
                    start = datetime.fromisoformat(segment["start"])
                    end = (
                        datetime.fromisoformat(segment["end"])
                        if segment["end"]
                        else now
                    )
                    days, _ = elapsed(start, end, holidays)
                    for threshold in MILESTONES:
                        if days >= threshold:
                            self.db.execute(
                                "INSERT OR IGNORE INTO awards VALUES (?, ?, ?)",
                                (
                                    uid,
                                    threshold,
                                    milestone_time(
                                        start, threshold, holidays
                                    ).isoformat(),
                                ),
                            )
                    # A promise made before this series ended may become earned
                    # when an erroneously excluded accounting day is restored.
                    self.db.execute(
                        "UPDATE gifts SET eligible_at=? WHERE user_id=? AND threshold<=? "
                        "AND created_at<=? AND eligible_at IS NULL AND cancelled_at IS NULL",
                        (now.isoformat(), uid, days, end.isoformat()),
                    )
                self._snapshot(uid, now)
                self._remember(key, {"done": True})

    def confirmation(self, uid, chat, now):
        with self.transaction():
            snapshot = self._snapshot(uid, now)
            if snapshot.segment_id is None:
                raise ValueError("Сначала задайте дату начала.")
            self.db.execute(
                "DELETE FROM confirmations WHERE expires<? OR actor=?",
                (now.timestamp(), uid),
            )
            token = secrets.token_hex(12)
            self.db.execute(
                "INSERT INTO confirmations VALUES (?, ?, ?, ?, ?)",
                (token, uid, chat, snapshot.segment_id, now.timestamp() + 300),
            )
            return token

    def cancel_confirmation(self, uid, chat):
        self.db.execute(
            "DELETE FROM confirmations WHERE actor=? AND chat=?", (uid, chat)
        )

    def reset(self, uid, chat, token, now, key, notifications):
        """Persist reset and its public notifications in the same transaction."""
        with self.transaction():
            repeated = self._repeat(key)
            if repeated is not None:
                return repeated
            confirmation = self.db.execute(
                "SELECT * FROM confirmations WHERE token=? AND actor=? AND chat=? AND expires>=?",
                (token, uid, chat, now.timestamp()),
            ).fetchone()
            before = self._snapshot(uid, now)
            if not confirmation or confirmation["segment"] != before.segment_id:
                raise ValueError("Подтверждение устарело. Откройте сброс заново.")
            self.db.execute("DELETE FROM confirmations WHERE actor=?", (uid,))
            self.db.execute("DELETE FROM dialogs WHERE actor=?", (uid,))
            self.db.execute(
                "UPDATE segments SET end=? WHERE id=?",
                (now.isoformat(), before.segment_id),
            )
            self.db.execute(
                "INSERT INTO segments(user_id,start) VALUES (?, ?)",
                (uid, now.isoformat()),
            )
            after = self._snapshot(uid, now)
            for payload in notifications(before, after):
                self.db.execute(
                    "INSERT INTO outbox(payload) VALUES (?)",
                    (json.dumps({"user_id": uid, "message": payload}),),
                )
            return self._remember(
                key, {"old_days": before.days, "old_hours": before.hours}
            )

    def add_gift(
        self, uid, donor, name, threshold, description, now, key, *, dialog=None
    ):
        if not 1 <= threshold <= 36500:
            raise ValueError("Введите число дней от 1 до 36500.")
        with self.transaction():
            result = self._repeat(key)
            if result is not None:
                return result["gift"]
            if (
                self.db.execute(
                    "SELECT COUNT(*) FROM gifts WHERE user_id=? AND cancelled_at IS NULL AND received_at IS NULL",
                    (uid,),
                ).fetchone()[0]
                >= 50
            ):
                raise ValueError("Для этого пользователя уже 50 открытых подарков.")
            cursor = self.db.execute(
                "INSERT INTO gifts(user_id,donor_id,donor_name,threshold,description,created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (uid, donor, name, threshold, description, now.isoformat()),
            )
            self._snapshot(uid, now)
            self._finish_input(key, {"gift": cursor.lastrowid}, dialog)
            return cursor.lastrowid

    def gifts(self, uid, page=0):
        return [
            dict(r)
            for r in self.db.execute(
                "SELECT * FROM gifts WHERE user_id=? ORDER BY id DESC LIMIT 5 OFFSET ?",
                (uid, max(0, page) * 5),
            )
        ]

    def receive_gift(self, uid, gift_id, now, key):
        with self.transaction():
            if self._repeat(key) is not None:
                return
            self._snapshot(uid, now)
            self.db.execute(
                "UPDATE gifts SET received_at=? WHERE id=? AND user_id=? "
                "AND eligible_at IS NOT NULL AND cancelled_at IS NULL AND received_at IS NULL",
                (now.isoformat(), gift_id, uid),
            )
            self._remember(key, {"done": True})

    def cancel_gift(self, uid, donor, gift_id, now, key):
        with self.transaction():
            if self._repeat(key) is not None:
                return
            self._snapshot(uid, now)
            self.db.execute(
                "UPDATE gifts SET cancelled_at=? WHERE id=? AND user_id=? AND donor_id=? "
                "AND eligible_at IS NULL AND received_at IS NULL AND cancelled_at IS NULL",
                (now.isoformat(), gift_id, uid, donor),
            )
            self._remember(key, {"done": True})

    def put_dialog(self, actor, chat, thread, kind, payload, prompt, now, key=None):
        if actor != chat:
            raise ValueError("Ввод доступен только в личке.")
        with self.transaction():
            if key and self.processed(key):
                return
            self.db.execute(
                "INSERT OR REPLACE INTO dialogs VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    actor,
                    chat,
                    thread or 0,
                    kind,
                    json.dumps(payload),
                    prompt,
                    now.timestamp() + DIALOG_TTL,
                ),
            )
            if payload.get("gift_token"):
                self._renew_invite(payload["gift_token"], actor, now)
            if key:
                self._remember(key, {"dialog": True})

    def set_prompt(self, actor, chat, thread, origin, prompt):
        with self.transaction():
            row = self.db.execute(
                "SELECT payload FROM dialogs WHERE actor=? AND chat=? AND thread=?",
                (actor, chat, thread or 0),
            ).fetchone()
            if row and json.loads(row[0]).get("origin") == origin:
                self.db.execute(
                    "UPDATE dialogs SET prompt=? WHERE actor=? AND chat=? AND thread=?",
                    (prompt, actor, chat, thread or 0),
                )

    def dialog(self, actor, chat, thread, now):
        self.db.execute("DELETE FROM dialogs WHERE expires<=?", (now.timestamp(),))
        row = self.db.execute(
            "SELECT * FROM dialogs WHERE actor=? AND chat=? AND thread=?",
            (actor, chat, thread or 0),
        ).fetchone()
        return dict(row) | {"payload": json.loads(row["payload"])} if row else None

    def clear_dialog(self, actor, chat, thread):
        self.db.execute(
            "DELETE FROM dialogs WHERE actor=? AND chat=? AND thread=?",
            (actor, chat, thread or 0),
        )

    def gift_invite(self, uid, actor, chat, thread, now, key):
        with self.transaction():
            repeated = self._repeat(key)
            if repeated is not None:
                return repeated["invite"]
            token = secrets.token_hex(12)
            value = {
                "uid": uid,
                "actor": actor,
                "chat": chat,
                "thread": thread,
                "expires": now.timestamp() + DIALOG_TTL,
            }
            self.db.execute(
                "INSERT INTO meta VALUES (?, ?)",
                (f"gift_invite:{token}", json.dumps(value)),
            )
            self._remember(key, {"invite": token})
            return token

    def invite(self, token, actor, now):
        row = self.db.execute(
            "SELECT value FROM meta WHERE key=?", (f"gift_invite:{token}",)
        ).fetchone()
        value = json.loads(row[0]) if row else None
        return (
            value
            if value and value["actor"] == actor and value["expires"] > now.timestamp()
            else None
        )

    def activate_gift(self, token, actor, now):
        if not self.invite(token, actor, now):
            raise ValueError("Ссылка устарела или предназначена другому пользователю.")
        self.db.execute(
            "INSERT OR REPLACE INTO meta VALUES (?, ?)", (f"gift_access:{actor}", token)
        )

    def gift_access(self, actor, now):
        row = self.db.execute(
            "SELECT value FROM meta WHERE key=?", (f"gift_access:{actor}",)
        ).fetchone()
        if not row:
            return None
        value = self.invite(row[0], actor, now)
        return value | {"token": row[0]} if value else None

    def clear_gift_access(self, actor):
        self.db.execute("DELETE FROM meta WHERE key=?", (f"gift_access:{actor}",))

    def _renew_invite(self, token, actor, now):
        value = self.invite(token, actor, now)
        if not value:
            raise ValueError(
                "Ссылка подарка устарела. Откройте подарок заново из группы."
            )
        value["expires"] = now.timestamp() + DIALOG_TTL
        self.db.execute(
            "UPDATE meta SET value=? WHERE key=?",
            (json.dumps(value), f"gift_invite:{token}"),
        )

    def expire_dialogs(self, now):
        self.db.execute(
            "DELETE FROM dialogs WHERE expires<=? OR chat<0", (now.timestamp(),)
        )
        for row in list(
            self.db.execute("SELECT key,value FROM meta WHERE key LIKE 'gift_invite:%'")
        ):
            if json.loads(row["value"])["expires"] <= now.timestamp():
                self.db.execute("DELETE FROM meta WHERE key=?", (row["key"],))
        for row in list(
            self.db.execute("SELECT key,value FROM meta WHERE key LIKE 'gift_access:%'")
        ):
            if not self.db.execute(
                "SELECT 1 FROM meta WHERE key=?", (f"gift_invite:{row['value']}",)
            ).fetchone():
                self.db.execute("DELETE FROM meta WHERE key=?", (row["key"],))

    def pending(self, now=None):
        items = []
        for row in self.db.execute(
            "SELECT outbox.*,meta.value AS retry FROM outbox LEFT JOIN meta ON meta.key='outbox_retry:' || outbox.id WHERE sent=0 ORDER BY id"
        ):
            retry = json.loads(row["retry"]) if row["retry"] else None
            if now and retry and retry["at"] > now.timestamp():
                continue
            items.append(dict(row) | {"payload": json.loads(row["payload"])})
            if len(items) == 20:
                break
        return items

    def retry(self, item_id, now, delay=None):
        with self.transaction():
            key = f"outbox_retry:{item_id}"
            row = self.db.execute(
                "SELECT value FROM meta WHERE key=?", (key,)
            ).fetchone()
            attempts = json.loads(row[0])["attempts"] + 1 if row else 1
            delay = delay or min(300, 2 ** min(attempts, 9))
            self.db.execute(
                "INSERT OR REPLACE INTO meta VALUES (?, ?)",
                (
                    key,
                    json.dumps({"attempts": attempts, "at": now.timestamp() + delay}),
                ),
            )
            return delay

    def sent(self, item_id):
        with self.transaction():
            self.db.execute("UPDATE outbox SET sent=1 WHERE id=?", (item_id,))
            self.db.execute(
                "DELETE FROM meta WHERE key=?", (f"outbox_retry:{item_id}",)
            )

    def offset(self):
        row = self.db.execute("SELECT value FROM meta WHERE key='offset'").fetchone()
        return int(row[0]) if row else None

    def advance(self, update_id):
        self.db.execute(
            "INSERT OR REPLACE INTO meta VALUES ('offset', ?)", (str(update_id + 1),)
        )

    def command_scopes(self):
        row = self.db.execute("SELECT value FROM meta WHERE key='scopes'").fetchone()
        return set(json.loads(row[0])) if row else set()

    def save_scopes(self, scopes):
        self.db.execute(
            "INSERT OR REPLACE INTO meta VALUES ('scopes', ?)",
            (json.dumps(sorted(scopes)),),
        )

    def tick(self, now, users=None):
        with self.transaction():
            self.expire_dialogs(now)
            for row in self.db.execute("SELECT id FROM users"):
                if users is None or row[0] in users:
                    self._snapshot(row[0], now.astimezone(UTC))
