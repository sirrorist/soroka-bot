"""handlers/group.py: лимит чата, смена дня, переезд группы в супергруппу, Premium чата.

База - настоящая SQLite (фикстура db из conftest), а не моки: условные UPDATE и
"строка уже есть" проверяются самой базой. Время замораживается подменой datetime
в модуле group, чтобы тесты не зависели от текущей даты.
"""

import asyncio
import datetime as real_dt
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from database import Chat
from handlers import group

MB = 1024 * 1024
NOW = real_dt.datetime(2026, 6, 15, 12, 0, 0)
TODAY = NOW.date()
YESTERDAY = TODAY - real_dt.timedelta(days=1)


@pytest.fixture(autouse=True)
def frozen(monkeypatch):
    monkeypatch.setattr(group, "utcnow", lambda: NOW)
    # asyncio.Lock привязывается к циклу событий при первом ожидании; у каждого теста
    # свой цикл, поэтому замок модуля на время теста заменяется свежим
    monkeypatch.setattr(group, "_migrate_lock", asyncio.Lock())


async def put(db, chat_id=-100, **fields):
    async with db() as s:
        s.add(Chat(id=chat_id, **fields))
        await s.commit()


async def row(db, chat_id=-100):
    async with db() as s:
        return await s.get(Chat, chat_id)


async def count_rows(db):
    async with db() as s:
        return len((await s.execute(group.select(Chat))).scalars().all())


# ---------- chat_is_premium ----------


def test_not_premium_flag_wins_over_future_term():
    chat = Chat(id=1, is_premium=False, premium_until=NOW + real_dt.timedelta(days=30))
    assert group.chat_is_premium(chat) is False


def test_premium_without_term_is_forever():
    assert (
        group.chat_is_premium(Chat(id=1, is_premium=True, premium_until=None)) is True
    )


def test_premium_with_future_term_is_active():
    chat = Chat(id=1, is_premium=True, premium_until=NOW + real_dt.timedelta(seconds=1))
    assert group.chat_is_premium(chat) is True


def test_premium_with_past_term_is_expired():
    chat = Chat(id=1, is_premium=True, premium_until=NOW - real_dt.timedelta(seconds=1))
    assert group.chat_is_premium(chat) is False


def test_premium_expires_exactly_at_term():
    # граница: premium_until == сейчас уже не действует (строгое сравнение)
    assert (
        group.chat_is_premium(Chat(id=1, is_premium=True, premium_until=NOW)) is False
    )


def test_null_flag_is_not_premium():
    assert not group.chat_is_premium(Chat(id=1, is_premium=None))


def test_chat_limits_by_plan(monkeypatch):
    monkeypatch.setattr(settings, "group_free_limit", 3)
    monkeypatch.setattr(settings, "group_free_daily_mb", 7)
    monkeypatch.setattr(settings, "premium_user_limit", 900)
    monkeypatch.setattr(settings, "group_premium_daily_mb", 11)
    assert group.chat_limits(False) == (3, 7 * MB)
    assert group.chat_limits(True) == (900, 11 * MB)


# ---------- _reserve_chat ----------


@pytest.mark.asyncio
async def test_reserve_stops_at_count_limit(db):
    await put(db, day=TODAY, downloads_today=0, bytes_today=0)
    got = [await group._reserve_chat(-100, 2, 1000 * MB) for _ in range(3)]
    assert got == [True, True, False]
    assert (await row(db)).downloads_today == 2


@pytest.mark.asyncio
async def test_reserve_refused_at_exactly_limit_does_not_change_counter(db):
    await put(db, day=TODAY, downloads_today=10, bytes_today=0)
    assert await group._reserve_chat(-100, 10, 1000 * MB) is False
    assert (await row(db)).downloads_today == 10


@pytest.mark.asyncio
async def test_reserve_by_bytes_boundary(db):
    daily = 350 * MB
    await put(db, chat_id=-1, day=TODAY, downloads_today=0, bytes_today=daily - 1)
    await put(db, chat_id=-2, day=TODAY, downloads_today=0, bytes_today=daily)
    await put(db, chat_id=-3, day=TODAY, downloads_today=0, bytes_today=daily + 5)
    # на байт меньше объёма - можно, ровно объём и больше - нельзя
    assert await group._reserve_chat(-1, 10, daily) is True
    assert await group._reserve_chat(-2, 10, daily) is False
    assert await group._reserve_chat(-3, 10, daily) is False
    assert (await row(db, -2)).downloads_today == 0


@pytest.mark.asyncio
async def test_reserve_unknown_chat_is_refused(db):
    assert await group._reserve_chat(-777, 10, 350 * MB) is False


@pytest.mark.asyncio
async def test_reserve_treats_null_counters_as_zero(db):
    await put(db, day=TODAY, downloads_today=None, bytes_today=None)
    assert await group._reserve_chat(-100, 1, 1 * MB) is True
    assert (await row(db)).downloads_today == 1


@pytest.mark.asyncio
async def test_reserve_new_day_resets_counters_before_counting(db):
    # вчера лимит был исчерпан и по числу, и по объёму
    await put(
        db, day=YESTERDAY, downloads_today=10, bytes_today=500 * MB, total_downloads=42
    )
    assert await group._reserve_chat(-100, 10, 350 * MB) is True
    r = await row(db)
    assert (r.day, r.downloads_today, r.bytes_today) == (TODAY, 1, 0)
    assert r.total_downloads == 42  # всего за всё время не сбрасывается


@pytest.mark.asyncio
async def test_reserve_same_day_does_not_reset(db):
    await put(db, day=TODAY, downloads_today=10, bytes_today=500 * MB)
    assert await group._reserve_chat(-100, 10, 350 * MB) is False
    r = await row(db)
    assert (r.downloads_today, r.bytes_today) == (10, 500 * MB)


@pytest.mark.asyncio
async def test_roll_day_without_day_resets(db):
    await put(db, day=None, downloads_today=4, bytes_today=9)
    assert await group._reserve_chat(-100, 10, 350 * MB) is True
    r = await row(db)
    assert (r.day, r.downloads_today, r.bytes_today) == (TODAY, 1, 0)


@pytest.mark.asyncio
async def test_roll_day_happens_once_per_day(db):
    await put(db, day=YESTERDAY, downloads_today=3, bytes_today=5)
    assert await group._reserve_chat(-100, 10, 350 * MB) is True
    assert await group._reserve_chat(-100, 10, 350 * MB) is True
    # второй вызов в тот же день не обнуляет заново
    assert (await row(db)).downloads_today == 2


@pytest.mark.asyncio
async def test_roll_day_touches_only_its_own_chat(db):
    await put(db, chat_id=-1, day=YESTERDAY, downloads_today=3)
    await put(db, chat_id=-2, day=YESTERDAY, downloads_today=3)
    await group._reserve_chat(-1, 10, 350 * MB)
    other = await row(db, -2)
    assert (other.day, other.downloads_today) == (YESTERDAY, 3)


# ---------- _release_chat ----------


@pytest.mark.asyncio
async def test_release_returns_one_slot(db):
    await put(db, day=TODAY, downloads_today=3)
    await group._release_chat(-100)
    assert (await row(db)).downloads_today == 2


@pytest.mark.asyncio
async def test_release_never_goes_below_zero(db):
    await put(db, day=TODAY, downloads_today=0)
    await group._release_chat(-100)
    await group._release_chat(-100)
    assert (await row(db)).downloads_today == 0


@pytest.mark.asyncio
async def test_release_unknown_chat_is_harmless(db):
    await group._release_chat(-777)
    assert await count_rows(db) == 0


# ---------- _chat_fits ----------


@pytest.mark.asyncio
async def test_fits_refuses_file_larger_than_remainder(db):
    await put(db, day=TODAY, bytes_today=300 * MB)
    text = await group._chat_fits(-100, 350 * MB, 60 * MB)
    assert text == "Файл 60 МБ не влезает в дневной объём чата - осталось 50 МБ."


@pytest.mark.asyncio
async def test_fits_allows_exactly_remainder(db):
    await put(db, day=TODAY, bytes_today=300 * MB)
    assert await group._chat_fits(-100, 350 * MB, 50 * MB) is None


@pytest.mark.asyncio
async def test_fits_refuses_one_byte_over(db):
    await put(db, day=TODAY, bytes_today=300 * MB)
    assert await group._chat_fits(-100, 350 * MB, 50 * MB + 1) is not None


@pytest.mark.asyncio
async def test_fits_with_null_bytes_counts_from_zero(db):
    await put(db, day=TODAY, bytes_today=None)
    assert await group._chat_fits(-100, 350 * MB, 350 * MB) is None
    assert await group._chat_fits(-100, 350 * MB, 350 * MB + 1) is not None


@pytest.mark.asyncio
async def test_fits_when_already_over_limit_reports_zero_left(db):
    await put(db, day=TODAY, bytes_today=400 * MB)
    text = await group._chat_fits(-100, 350 * MB, 1 * MB)
    # остаток не уходит в минус: "осталось -50" пользователю не показывают
    assert (
        text is not None
        and "осталось 0" in text
        and "-" not in text.split("осталось")[1]
    )


@pytest.mark.asyncio
async def test_fits_english_text_uses_dot_and_en_units(db):
    from utils.i18n import set_lang

    set_lang("en")
    await put(db, day=TODAY, bytes_today=0)
    text = await group._chat_fits(-100, 5 * MB, int(6.5 * MB))
    assert (
        text
        == "The file is 6.5 MB and doesn't fit the chat's daily volume - 5.0 MB left."
    )


# ---------- _chat_delivered ----------


@pytest.mark.asyncio
async def test_delivered_adds_bytes_and_total(db):
    await put(db, day=TODAY, bytes_today=10, total_downloads=5)
    await group._chat_delivered(-100, 100)
    await group._chat_delivered(-100, 20)
    r = await row(db)
    assert (r.bytes_today, r.total_downloads) == (130, 7)


@pytest.mark.asyncio
async def test_delivered_with_null_counters(db):
    await put(db, day=TODAY, bytes_today=None, total_downloads=None)
    await group._chat_delivered(-100, 100)
    r = await row(db)
    assert (r.bytes_today, r.total_downloads) == (100, 1)


@pytest.mark.asyncio
async def test_delivered_does_not_touch_downloads_today(db):
    # слот занят в _reserve_chat заранее; после отправки он не прибавляется второй раз
    await put(db, day=TODAY, downloads_today=1)
    await group._chat_delivered(-100, 100)
    assert (await row(db)).downloads_today == 1


# ---------- get_chat ----------


def tg_chat(chat_id=-100, title="Чат"):
    return SimpleNamespace(id=chat_id, title=title)


@pytest.mark.asyncio
async def test_get_chat_creates_row_for_today(db):
    chat = await group.get_chat(tg_chat(title="Друзья"))
    assert (chat.id, chat.title, chat.day) == (-100, "Друзья", TODAY)
    assert (await row(db)).title == "Друзья"


@pytest.mark.asyncio
async def test_get_chat_updates_changed_title(db):
    await put(db, day=TODAY, title="Старое")
    chat = await group.get_chat(tg_chat(title="Новое"))
    assert chat.title == "Новое"
    assert (await row(db)).title == "Новое"


@pytest.mark.asyncio
async def test_get_chat_resets_counters_on_new_day(db):
    await put(db, day=YESTERDAY, downloads_today=9, bytes_today=99, title="Чат")
    chat = await group.get_chat(tg_chat())
    assert (chat.downloads_today, chat.bytes_today, chat.day) == (0, 0, TODAY)


@pytest.mark.asyncio
async def test_two_first_commands_at_once_create_one_row(db):
    a, b = await asyncio.gather(group.get_chat(tg_chat()), group.get_chat(tg_chat()))
    assert a.id == b.id == -100
    assert await count_rows(db) == 1


# ---------- migrate_chat ----------

OLD, NEW = -500, -1000500
UNTIL = real_dt.datetime(2026, 12, 31, 0, 0, 0)


@pytest.mark.asyncio
async def test_migrate_moves_premium_and_counters_to_new_id(db):
    await put(
        db,
        OLD,
        title="Старая",
        is_premium=True,
        premium_until=UNTIL,
        day=TODAY,
        downloads_today=4,
        bytes_today=123,
        total_downloads=17,
    )
    await group.migrate_chat(OLD, NEW, "Супергруппа")
    assert await row(db, OLD) is None
    n = await row(db, NEW)
    assert n.title == "Супергруппа"
    assert (n.is_premium, n.premium_until) == (True, UNTIL)
    assert (n.day, n.downloads_today, n.bytes_today, n.total_downloads) == (
        TODAY,
        4,
        123,
        17,
    )


@pytest.mark.asyncio
async def test_migrate_keeps_old_title_when_new_has_none(db):
    await put(db, OLD, title="Старая", day=TODAY)
    await group.migrate_chat(OLD, NEW, None)
    assert (await row(db, NEW)).title == "Старая"


@pytest.mark.asyncio
async def test_migrate_second_call_does_nothing(db):
    await put(
        db, OLD, is_premium=True, premium_until=UNTIL, day=TODAY, total_downloads=17
    )
    await group.migrate_chat(OLD, NEW, "Т")
    await group.migrate_chat(OLD, NEW, "Т")
    n = await row(db, NEW)
    assert n.total_downloads == 17
    assert await count_rows(db) == 1


@pytest.mark.asyncio
async def test_migrate_reverse_pair_after_first_is_noop(db):
    # Telegram шлёт два сообщения, и оба зовут migrate_chat(old, new) - второе идентично
    await put(db, OLD, day=TODAY, total_downloads=3)
    await group.migrate_chat(OLD, NEW, None)
    await group.migrate_chat(OLD, NEW, "Т")
    assert (await row(db, NEW)).total_downloads == 3


@pytest.mark.asyncio
async def test_migrate_unknown_old_chat_creates_nothing(db):
    await group.migrate_chat(OLD, NEW, "Т")
    assert await count_rows(db) == 0


@pytest.mark.asyncio
async def test_migrate_into_existing_row_carries_premium_and_sums_total(db):
    await put(
        db,
        OLD,
        is_premium=True,
        premium_until=UNTIL,
        day=TODAY,
        total_downloads=3,
        downloads_today=4,
        bytes_today=40,
    )
    await put(
        db,
        NEW,
        is_premium=False,
        day=TODAY,
        total_downloads=5,
        downloads_today=1,
        bytes_today=10,
    )
    await group.migrate_chat(OLD, NEW, None)
    assert await row(db, OLD) is None
    n = await row(db, NEW)
    assert (n.is_premium, n.premium_until) == (True, UNTIL)
    assert n.total_downloads == 8
    # счётчики дня новой строки остаются её собственными
    assert (n.downloads_today, n.bytes_today) == (1, 10)


@pytest.mark.asyncio
async def test_migrate_does_not_replace_active_premium_of_new_row(db):
    later = real_dt.datetime(2027, 6, 1)
    await put(db, OLD, is_premium=True, premium_until=UNTIL, day=TODAY)
    await put(db, NEW, is_premium=True, premium_until=later, day=TODAY)
    await group.migrate_chat(OLD, NEW, None)
    assert (await row(db, NEW)).premium_until == later


@pytest.mark.asyncio
async def test_migrate_does_not_carry_expired_premium_into_existing_row(db):
    expired = NOW - real_dt.timedelta(days=1)
    await put(db, OLD, is_premium=True, premium_until=expired, day=TODAY)
    await put(db, NEW, is_premium=False, day=TODAY)
    await group.migrate_chat(OLD, NEW, None)
    assert (await row(db, NEW)).is_premium is False


@pytest.mark.asyncio
async def test_migrate_treats_null_totals_as_zero(db):
    await put(db, OLD, day=TODAY, total_downloads=None)
    await put(db, NEW, day=TODAY, total_downloads=None)
    await group.migrate_chat(OLD, NEW, None)
    assert (await row(db, NEW)).total_downloads == 0


@pytest.mark.parametrize("new_exists", [False, True], ids=["new-absent", "new-exists"])
@pytest.mark.asyncio
async def test_two_simultaneous_migrations_transfer_once(db, monkeypatch, new_exists):
    """Обе служебные записи о переезде приходят одновременно (был баг: счётчики
    переносились дважды). Второй читатель новой строки задерживается так, чтобы
    увидеть её уже после коммита первого - без замка это и даёт двойной перенос."""
    await put(
        db, OLD, is_premium=True, premium_until=UNTIL, day=TODAY, total_downloads=3
    )
    if new_exists:
        await put(db, NEW, is_premium=False, day=TODAY, total_downloads=5)

    real_get = AsyncSession.get
    seen = {"new_reads": 0}

    async def slow_get(self, entity, ident, *args, **kwargs):
        if entity is Chat and ident == NEW:
            seen["new_reads"] += 1
            if seen["new_reads"] == 2:
                await asyncio.sleep(0.3)
        return await real_get(self, entity, ident, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "get", slow_get)

    await asyncio.gather(
        group.migrate_chat(OLD, NEW, None),
        group.migrate_chat(OLD, NEW, "Т"),
    )

    assert await row(db, OLD) is None
    n = await row(db, NEW)
    assert n.total_downloads == (8 if new_exists else 3)
    assert n.is_premium is True and n.premium_until == UNTIL
    assert await count_rows(db) == 1


@pytest.mark.asyncio
async def test_migrate_service_messages_call_migrate_with_right_ids(monkeypatch):
    migrate = AsyncMock()
    monkeypatch.setattr(group, "migrate_chat", migrate)

    to_msg = SimpleNamespace(
        chat=SimpleNamespace(id=-500, title="Старая"), migrate_to_chat_id=-1000500
    )
    await group.on_migrate_to(to_msg)
    migrate.assert_awaited_with(-500, -1000500, None)

    from_msg = SimpleNamespace(
        chat=SimpleNamespace(id=-1000500, title="Новая"), migrate_from_chat_id=-500
    )
    await group.on_migrate_from(from_msg)
    migrate.assert_awaited_with(-500, -1000500, "Новая")


# ---------- group_download: связка команды с лимитом чата ----------


def group_message(chat_id=-100):
    m = MagicMock()
    m.chat = SimpleNamespace(id=chat_id, title="Чат")
    m.answer = AsyncMock()
    m.reply_to_message = None
    return m


def command(args):
    return SimpleNamespace(args=args, command="sr_dl")


@pytest.mark.asyncio
async def test_group_dl_without_user_is_skipped(db, monkeypatch):
    deliver = AsyncMock()
    monkeypatch.setattr(group.dl, "deliver", deliver)
    m = group_message()
    await group.group_download(m, command("https://youtu.be/abc"), user=None)
    deliver.assert_not_awaited()
    m.answer.assert_not_awaited()


@pytest.mark.asyncio
async def test_group_dl_without_link_answers_usage_with_prefix(db, monkeypatch):
    deliver = AsyncMock()
    monkeypatch.setattr(group.dl, "deliver", deliver)
    m = group_message()
    await group.group_download(m, command(None), user=SimpleNamespace(id=1))
    deliver.assert_not_awaited()
    text = m.answer.await_args.args[0]
    assert "/sr_dl ссылка" in text


@pytest.mark.asyncio
async def test_group_dl_link_from_replied_message(db, monkeypatch):
    deliver = AsyncMock()
    monkeypatch.setattr(group.dl, "deliver", deliver)
    m = group_message()
    m.reply_to_message = SimpleNamespace(
        text=None, caption="глянь https://youtu.be/abc"
    )
    await group.group_download(m, command(None), user=SimpleNamespace(id=1))
    kw = deliver.await_args.kwargs
    assert (kw["platform"], kw["url"]) == ("youtube", "https://youtu.be/abc")


@pytest.mark.asyncio
async def test_group_dl_free_chat_wires_chat_limits_and_upsell(db, monkeypatch):
    monkeypatch.setattr(settings, "group_free_limit", 2)
    monkeypatch.setattr(settings, "group_free_daily_mb", 350)
    deliver = AsyncMock()
    monkeypatch.setattr(group.dl, "deliver", deliver)
    await group.group_download(
        group_message(), command("https://youtu.be/abc"), user=SimpleNamespace(id=7)
    )

    kw = deliver.await_args.kwargs
    assert kw["user_id"] == 7 and kw["premium"] is False
    assert "2 скачивания или 350 МБ" in kw["limit_text"]
    assert "/sr_premium" in kw["limit_text"]  # свободному чату предлагают Premium

    # reserve/release/fits/on_delivered ходят в строку ЧАТА, а не пользователя
    assert await kw["reserve"]() is True
    assert await kw["reserve"]() is True
    assert await kw["reserve"]() is False
    assert (await row(db)).downloads_today == 2
    await kw["release"]()
    assert (await row(db)).downloads_today == 1
    await kw["on_delivered"](100 * MB)
    r = await row(db)
    assert (r.bytes_today, r.total_downloads) == (100 * MB, 1)
    assert await kw["fits"](250 * MB) is None
    assert await kw["fits"](250 * MB + 1) is not None


@pytest.mark.asyncio
async def test_group_dl_premium_chat_has_premium_limits_and_no_upsell(db, monkeypatch):
    await put(db, day=TODAY, is_premium=True, premium_until=None, title="Чат")
    monkeypatch.setattr(settings, "premium_user_limit", 1000)
    monkeypatch.setattr(settings, "group_premium_daily_mb", 5120)
    deliver = AsyncMock()
    monkeypatch.setattr(group.dl, "deliver", deliver)
    await group.group_download(
        group_message(), command("https://youtu.be/abc"), user=SimpleNamespace(id=7)
    )

    kw = deliver.await_args.kwargs
    assert kw["premium"] is True
    assert "1000 скачиваний или 5 ГБ" in kw["limit_text"]
    assert "/sr_premium" not in kw["limit_text"]
