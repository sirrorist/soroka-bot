"""middleware.py: сброс дневного счётчика, лимит, язык, лог команд.

База - SQLite (фикстура db из conftest). Время замораживается подменой datetime
в модуле middleware.
"""

import datetime as real_dt
from types import SimpleNamespace

import pytest
from aiogram.types import User as TgUser
from loguru import logger

import middleware
from config import settings
from database import User, UserSetting
from utils import lang_store
from utils.i18n import get_lang, set_lang


def freeze(monkeypatch, now):
    monkeypatch.setattr(middleware, "utcnow", lambda: now)


class Probe:
    """Обработчик-свидетель: запоминает, что получил, и язык на момент вызова."""

    def __init__(self):
        self.calls = 0
        self.data = None
        self.lang = None

    async def __call__(self, event, data):
        self.calls += 1
        self.data = data
        self.lang = get_lang()
        return "handled"


def tg_user(user_id=5, **kw):
    kw.setdefault("first_name", "Аня")
    return TgUser(id=user_id, is_bot=False, **kw)


async def stored(db, user_id=5):
    async with db() as s:
        return await s.get(User, user_id)


# ---------- UserMiddleware ----------

NOW = real_dt.datetime(2026, 6, 15, 0, 0, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "last, now, expected",
    [
        # секунда назад, но вчера по UTC - новый день
        (
            real_dt.datetime(2026, 6, 14, 23, 59, 59),
            real_dt.datetime(2026, 6, 15, 0, 0, 1),
            0,
        ),
        (
            real_dt.datetime(2026, 6, 14, 0, 0, 0),
            real_dt.datetime(2026, 6, 15, 12, 0, 0),
            0,
        ),
        (
            real_dt.datetime(2025, 1, 1, 9, 0, 0),
            real_dt.datetime(2026, 6, 15, 12, 0, 0),
            0,
        ),
        # тот же день: от полуночи до конца дня счётчик не трогается
        (
            real_dt.datetime(2026, 6, 15, 0, 0, 0),
            real_dt.datetime(2026, 6, 15, 23, 59, 59),
            6,
        ),
        (
            real_dt.datetime(2026, 6, 15, 12, 0, 0),
            real_dt.datetime(2026, 6, 15, 12, 0, 1),
            6,
        ),
        # последняя активность "завтра" (часы отстали) - не новый день
        (
            real_dt.datetime(2026, 6, 16, 1, 0, 0),
            real_dt.datetime(2026, 6, 15, 23, 0, 0),
            6,
        ),
    ],
    ids=[
        "23:59-to-00:00",
        "yesterday",
        "year-ago",
        "same-day-midnight",
        "same-day",
        "clock-behind",
    ],
)
async def test_user_counter_resets_only_when_utc_day_changed(
    db, monkeypatch, last, now, expected
):
    async with db() as s:
        s.add(
            User(
                id=5,
                first_name="Аня",
                downloads_today=6,
                total_downloads=40,
                last_activity=last,
            )
        )
        await s.commit()
    freeze(monkeypatch, now)
    probe = Probe()

    await middleware.UserMiddleware()(probe, object(), {"event_from_user": tg_user()})

    assert probe.data["user"].downloads_today == expected
    row = await stored(db)
    assert row.downloads_today == expected
    assert row.last_activity == now
    assert row.total_downloads == 40  # "всего" не сбрасывается никогда


@pytest.mark.asyncio
async def test_user_reset_is_visible_to_rate_limit_that_runs_after(db, monkeypatch):
    """Регрессия: раньше дату сверял RateLimitMiddleware уже после её обновления и
    не сбрасывал счётчик никогда - человек, исчерпавший лимит вчера, оставался заблокирован."""
    monkeypatch.setattr(settings, "free_user_limit", 7)
    async with db() as s:
        s.add(
            User(
                id=5,
                downloads_today=7,
                last_activity=real_dt.datetime(2026, 6, 14, 20, 0, 0),
            )
        )
        await s.commit()
    freeze(monkeypatch, real_dt.datetime(2026, 6, 15, 9, 0, 0))
    probe = Probe()

    async def limited(event, data):
        return await middleware.RateLimitMiddleware()(probe, event, data)

    await middleware.UserMiddleware()(limited, object(), {"event_from_user": tg_user()})
    assert probe.data["rate_limited"] is False

    # и в тот же день лимит по-прежнему держит
    async with db() as s:
        u = await s.get(User, 5)
        u.downloads_today = 7
        await s.commit()
    freeze(monkeypatch, real_dt.datetime(2026, 6, 15, 18, 0, 0))
    await middleware.UserMiddleware()(limited, object(), {"event_from_user": tg_user()})
    assert probe.data["rate_limited"] is True


@pytest.mark.asyncio
async def test_new_user_is_created_with_telegram_fields(db, monkeypatch):
    freeze(monkeypatch, NOW)
    probe = Probe()
    tg = tg_user(7, username="anya", last_name="К", is_premium=True)

    result = await middleware.UserMiddleware()(probe, object(), {"event_from_user": tg})

    assert result == "handled" and probe.calls == 1
    row = await stored(db, 7)
    assert (row.username, row.first_name, row.last_name) == ("anya", "Аня", "К")
    assert row.is_premium is True
    assert row.downloads_today == 0
    assert probe.data["user"].id == 7


@pytest.mark.asyncio
async def test_new_user_without_premium_flag_is_not_premium(db, monkeypatch):
    freeze(monkeypatch, NOW)
    await middleware.UserMiddleware()(
        Probe(), object(), {"event_from_user": tg_user(8)}
    )
    assert (await stored(db, 8)).is_premium is False


@pytest.mark.asyncio
async def test_existing_user_profile_is_refreshed(db, monkeypatch):
    async with db() as s:
        s.add(User(id=5, username="old", first_name="Старое", last_activity=NOW))
        await s.commit()
    freeze(monkeypatch, NOW)

    await middleware.UserMiddleware()(
        Probe(), object(), {"event_from_user": tg_user(username="new")}
    )

    row = await stored(db)
    assert (row.username, row.first_name) == ("new", "Аня")


@pytest.mark.asyncio
async def test_premium_from_bot_database_is_not_lost_when_telegram_says_no(
    db, monkeypatch
):
    # Premium выдан администратором в базе; у Telegram-профиля его нет
    async with db() as s:
        s.add(User(id=5, is_premium=True, last_activity=NOW))
        await s.commit()
    freeze(monkeypatch, NOW)
    probe = Probe()

    await middleware.UserMiddleware()(probe, object(), {"event_from_user": tg_user()})

    assert probe.data["user"].is_premium is True
    assert (await stored(db)).is_premium is True


@pytest.mark.asyncio
async def test_event_without_user_passes_through_without_database(monkeypatch):
    def boom():
        raise AssertionError("база не нужна")

    monkeypatch.setattr(middleware, "session_scope", boom)
    probe = Probe()
    assert await middleware.UserMiddleware()(probe, object(), {}) == "handled"
    assert "user" not in probe.data


# ---------- RateLimitMiddleware ----------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "today, premium, limited",
    [
        (6, False, False),
        (7, False, True),
        (8, False, True),
        (7, True, False),
        (999, True, False),
        (1000, True, True),
    ],
)
async def test_rate_limit_boundary(monkeypatch, today, premium, limited):
    monkeypatch.setattr(settings, "free_user_limit", 7)
    monkeypatch.setattr(settings, "premium_user_limit", 1000)
    probe = Probe()
    user = SimpleNamespace(downloads_today=today, is_premium=premium)

    await middleware.RateLimitMiddleware()(probe, object(), {"user": user})

    assert probe.data["rate_limited"] is limited


@pytest.mark.asyncio
async def test_rate_limit_without_user_does_not_decide():
    probe = Probe()
    await middleware.RateLimitMiddleware()(probe, object(), {})
    assert "rate_limited" not in probe.data


# ---------- LangMiddleware / lang_store ----------


@pytest.mark.asyncio
async def test_lang_defaults_to_russian(db):
    probe = Probe()
    await middleware.LangMiddleware()(probe, object(), {"event_from_user": tg_user(11)})
    assert probe.lang == "ru"


@pytest.mark.asyncio
async def test_lang_saved_english_is_used_for_that_user_only(db):
    await lang_store.save_user_lang(11, "en")
    mine, other = Probe(), Probe()
    await middleware.LangMiddleware()(mine, object(), {"event_from_user": tg_user(11)})
    await middleware.LangMiddleware()(other, object(), {"event_from_user": tg_user(12)})
    assert (mine.lang, other.lang) == ("en", "ru")


@pytest.mark.asyncio
async def test_lang_choice_survives_process_restart(db):
    await lang_store.save_user_lang(11, "en")
    lang_store._cache.clear()  # как после перезапуска: кэш пуст, остаётся только база
    probe = Probe()
    await middleware.LangMiddleware()(probe, object(), {"event_from_user": tg_user(11)})
    assert probe.lang == "en"


@pytest.mark.asyncio
async def test_lang_previous_update_language_does_not_leak_into_event_without_user(db):
    set_lang("en")
    probe = Probe()
    await middleware.LangMiddleware()(probe, object(), {})
    assert probe.lang == "ru"


@pytest.mark.asyncio
async def test_lang_unknown_or_empty_value_in_database_falls_back_to_russian(db):
    async with db() as s:
        s.add(UserSetting(user_id=21, lang="de"))
        s.add(UserSetting(user_id=22, lang=None))
        await s.commit()
    for uid in (21, 22):
        # и само хранилище отдаёт ru, а не полагается на защиту в set_lang
        assert await lang_store.get_user_lang(uid) == "ru"
        probe = Probe()
        await middleware.LangMiddleware()(
            probe, object(), {"event_from_user": tg_user(uid)}
        )
        assert probe.lang == "ru"


@pytest.mark.asyncio
async def test_save_user_lang_twice_updates_single_row(db):
    await lang_store.save_user_lang(11, "en")
    await lang_store.save_user_lang(11, "ru")
    lang_store._cache.clear()
    assert await lang_store.get_user_lang(11) == "ru"
    async with db() as s:
        rows = (await s.execute(UserSetting.__table__.select())).all()
    assert len(rows) == 1


# ---------- CommandLogMiddleware ----------


@pytest.fixture
def log_lines():
    lines = []
    sink = logger.add(lambda m: lines.append(m.record["message"]), level="INFO")
    yield lines
    logger.remove(sink)


@pytest.mark.asyncio
async def test_command_log_records_command_and_chat_but_not_arguments(log_lines):
    event = SimpleNamespace(
        text="/sr_dl https://youtu.be/secret-video",
        chat=SimpleNamespace(type="supergroup", id=-100),
    )
    probe = Probe()
    await middleware.CommandLogMiddleware()(probe, event, {})
    assert probe.calls == 1
    assert log_lines == ["Command /sr_dl in supergroup chat -100"]


@pytest.mark.asyncio
async def test_command_log_ignores_plain_text_but_still_calls_handler(log_lines):
    probe = Probe()
    await middleware.CommandLogMiddleware()(
        probe,
        SimpleNamespace(text="привет", chat=SimpleNamespace(type="private", id=1)),
        {},
    )
    await middleware.CommandLogMiddleware()(
        probe, SimpleNamespace(text=None, chat=None), {}
    )
    assert probe.calls == 2
    assert log_lines == []


@pytest.mark.asyncio
async def test_command_log_truncates_long_command(log_lines):
    event = SimpleNamespace(
        text="/" + "a" * 100, chat=SimpleNamespace(type="private", id=1)
    )
    await middleware.CommandLogMiddleware()(Probe(), event, {})
    assert log_lines == ["Command /" + "a" * 39 + " in private chat 1"]
