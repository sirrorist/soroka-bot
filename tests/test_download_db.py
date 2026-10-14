"""Личный лимит и статистика на настоящей базе: _reserve_download, _release_download, show_stats.

Раньше эти места проверялись только на моках сессии: условный UPDATE и агрегаты
считала подделка, а не база, и незакрытую сессию мок не видел. Здесь - SQLite из
фикстуры db; её страховка в teardown (все сессии закрыты, соединения возвращены)
ловит утечку в любом из трёх мест.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import update

from config import settings
from database import Download, User
from handlers import download as dl
from handlers import stats

MB = 1024 * 1024


@pytest.fixture(autouse=True)
def limits(monkeypatch):
    # тексты статистики показывают лимиты: значение из окружения разработчика не мешает
    monkeypatch.setattr(settings, "free_user_limit", 7)
    monkeypatch.setattr(settings, "premium_user_limit", 1000)


async def put_user(db, user_id=5, **fields):
    fields.setdefault("first_name", "Аня")
    # None в конструкторе ORM заменяет значением по умолчанию (0), а не пишет NULL;
    # настоящий NULL - как у строк, созданных до появления столбца, - только UPDATE-ом
    nulls = [name for name, value in fields.items() if value is None]
    async with db() as s:
        s.add(User(id=user_id, **{k: v for k, v in fields.items() if v is not None}))
        await s.commit()
        if nulls:
            await s.execute(
                update(User).where(User.id == user_id).values(dict.fromkeys(nulls))
            )
            await s.commit()


async def counter(db, user_id=5):
    async with db() as s:
        return (await s.get(User, user_id)).downloads_today


# ---------- _reserve_download ----------


@pytest.mark.asyncio
async def test_reserve_download_takes_slots_until_limit_then_refuses(db):
    await put_user(db, downloads_today=0)

    results = [await dl._reserve_download(5, 3) for _ in range(4)]

    assert results == [True, True, True, False]
    assert await counter(db) == 3


@pytest.mark.asyncio
async def test_reserve_download_below_limit_by_one_takes_last_slot(db):
    await put_user(db, downloads_today=6)

    assert await dl._reserve_download(5, 7) is True
    assert await counter(db) == 7


@pytest.mark.asyncio
async def test_reserve_download_at_limit_is_refused_and_counter_unchanged(db):
    await put_user(db, downloads_today=7)

    assert await dl._reserve_download(5, 7) is False
    assert await counter(db) == 7


@pytest.mark.asyncio
async def test_reserve_download_over_limit_is_refused_and_counter_unchanged(db):
    # лимит снизили, а у пользователя уже больше: счётчик не уменьшается и не растёт
    await put_user(db, downloads_today=9)

    assert await dl._reserve_download(5, 7) is False
    assert await counter(db) == 9


@pytest.mark.asyncio
async def test_reserve_download_null_counter_counts_as_zero(db):
    await put_user(db, downloads_today=None)

    assert await dl._reserve_download(5, 7) is True
    assert await counter(db) == 1


@pytest.mark.asyncio
async def test_reserve_download_unknown_user_is_refused_and_creates_nothing(db):
    assert await dl._reserve_download(404, 7) is False

    async with db() as s:
        assert await s.get(User, 404) is None


@pytest.mark.asyncio
async def test_reserve_download_does_not_touch_other_users(db):
    await put_user(db, 5, downloads_today=0)
    await put_user(db, 6, downloads_today=2)

    assert await dl._reserve_download(5, 7) is True

    assert await counter(db, 5) == 1
    assert await counter(db, 6) == 2


# ---------- _release_download ----------


@pytest.mark.asyncio
async def test_release_download_returns_one_slot(db):
    await put_user(db, downloads_today=3)

    await dl._release_download(5)

    assert await counter(db) == 2


@pytest.mark.asyncio
async def test_release_download_never_goes_below_zero(db):
    await put_user(db, downloads_today=0)

    await dl._release_download(5)
    await dl._release_download(5)

    assert await counter(db) == 0


@pytest.mark.asyncio
async def test_release_download_null_counter_stays_null(db):
    await put_user(db, downloads_today=None)

    await dl._release_download(5)

    assert await counter(db) is None


@pytest.mark.asyncio
async def test_release_download_unknown_user_is_harmless_and_others_untouched(db):
    await put_user(db, 6, downloads_today=2)

    await dl._release_download(404)

    assert await counter(db, 6) == 2
    async with db() as s:
        assert await s.get(User, 404) is None


@pytest.mark.asyncio
async def test_reserve_then_release_restores_counter(db):
    await put_user(db, downloads_today=4)

    assert await dl._reserve_download(5, 7) is True
    await dl._release_download(5)

    assert await counter(db) == 4


# ---------- show_stats ----------


async def put_download(db, user_id, platform, size):
    async with db() as s:
        s.add(
            Download(
                user_id=user_id,
                platform=platform,
                url="https://example.com/v",
                content_type="video",
                file_size=size,
            )
        )
        await s.commit()


def command_message():
    # без атрибута message: ветка команды, не callback
    return SimpleNamespace(answer=AsyncMock())


@pytest.mark.asyncio
async def test_stats_counts_only_own_downloads_and_groups_by_platform(db):
    await put_user(db, 5, downloads_today=3, total_downloads=4)
    await put_user(db, 6, first_name="Чужой", downloads_today=1)
    for platform, size in [
        ("tiktok", 1 * MB),
        ("tiktok", 2 * MB),
        ("youtube", 3 * MB),
        ("tiktok", 5 * MB),
    ]:
        await put_download(db, 5, platform, size)
    # чужая история не должна попасть ни в число, ни в объём, ни в площадки
    await put_download(db, 6, "twitter", 100 * MB)
    message = command_message()

    await stats.show_stats(message, user=SimpleNamespace(id=5))

    text = message.answer.await_args.args[0]
    assert text == (
        "<b>📊 Ваша статистика</b>\n"
        "Аня · бесплатный тариф\n\n"
        "<b>Сегодня:</b>\n"
        "▰▰▰▰▱▱▱▱▱▱ <b>3</b> из <b>7</b>\n"
        "Осталось <b>4</b> · Обновится в 3:00 по Москве\n\n"
        "<b>За всё время</b>\n"
        "<b>4</b> скачивания · <b>11</b> МБ\n\n"
        "<b>Площадки</b>\n"
        "TikTok - <b>3</b>\n"
        "YouTube - <b>1</b>"
    )
    assert message.answer.await_args.kwargs["reply_markup"] is not None


@pytest.mark.asyncio
async def test_stats_without_downloads_says_nothing_downloaded_yet(db):
    await put_user(db, 5, downloads_today=None)
    # чужая история есть, у пользователя - нет
    await put_user(db, 6, first_name="Чужой")
    await put_download(db, 6, "tiktok", MB)
    message = command_message()

    await stats.show_stats(message, user=SimpleNamespace(id=5))

    text = message.answer.await_args.args[0]
    assert "Пока ничего не скачано - пришлите ссылку." in text
    assert "За всё время" not in text
    assert "Площадки" not in text
    assert "<b>0</b> из <b>7</b>" in text


@pytest.mark.asyncio
async def test_stats_premium_user_has_no_remainder_and_no_premium_button(db):
    await put_user(db, 5, is_premium=True, downloads_today=2)
    await put_download(db, 5, "instagram", MB)
    message = command_message()

    await stats.show_stats(message, user=SimpleNamespace(id=5))

    text = message.answer.await_args.args[0]
    assert "Аня · ⭐ Premium" in text
    assert "<b>2</b> из <b>1000</b>" in text
    assert "Осталось" not in text
    assert "Instagram - <b>1</b>" in text
    assert message.answer.await_args.kwargs["reply_markup"] is None
