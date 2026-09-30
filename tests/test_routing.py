"""Маршрутизация aiogram: какой обработчик получает сообщение в личке и в группе.

Без сети и без базы: настоящий Dispatcher, настоящая register_handlers, а обработчики
не исполняются - middleware-шпион только записывает имя и не зовёт их (как в
Docs/projects/tiktube-download-bot/recon/route_check.py).

register_handlers меняет роутеры модулей (навешивает фильтры и подключает их к
Dispatcher), а второй раз подключить роутер нельзя - aiogram скажет "Router is already
attached". Ту же register_handlers зовёт main.main(), которую гоняет test_stats_main.py,
поэтому фикстура dp отцепляет роутеры и перед собой (вдруг их уже занял другой тест),
и после себя. Без этого набор красный в одном из двух порядков.
"""
import datetime

import pytest
import pytest_asyncio
from aiogram import Bot, Dispatcher
from aiogram.types import CallbackQuery, Chat, Message, Update
from aiogram.types import User as TgUser

from handlers import download, group, language, premium, register_handlers, start, stats, steam

PRIVATE = Chat(id=1, type="private")
GROUP = Chat(id=-100, type="group", title="g")
SUPERGROUP = Chat(id=-1000100, type="supergroup", title="sg")

hits: list[str] = []


async def spy(handler, event, data):
    hits.append(data["handler"].callback.__name__)


def detach_routers():
    """Роутеры модулей - глобальные объекты: отцепить от прежнего Dispatcher."""
    for module in (group, start, stats, premium, steam, language, download):
        module.router._parent_router = None


@pytest.fixture(scope="module")
def dp():
    detach_routers()
    dispatcher = Dispatcher()
    dispatcher.message.middleware(spy)
    dispatcher.callback_query.middleware(spy)
    register_handlers(dispatcher)
    yield dispatcher
    detach_routers()


@pytest_asyncio.fixture
async def bot():
    b = Bot("1:x")
    # get_me в тестах не должен ходить в сеть: команды вида /cmd@бот проверяют имя бота
    b._me = TgUser(id=1, is_bot=True, first_name="Soroka", username="soroka_bot")
    yield b
    await b.session.close()


async def route(dp, bot, chat, text=None, **fields):
    hits.clear()
    msg = Message(
        message_id=1,
        date=datetime.datetime(2026, 1, 1),
        text=text,
        chat=chat,
        from_user=TgUser(id=1, is_bot=False, first_name="t"),
        **fields,
    )
    await dp.feed_update(bot, Update(update_id=1, message=msg))
    return list(hits)


LINK = "https://youtu.be/x"

PRIVATE_ROUTES = [
    ("/start", ["cmd_start"]),
    ("/help", ["cmd_help"]),
    ("/about", ["cmd_about"]),
    ("/download", ["download_menu"]),
    ("/stats", ["show_stats"]),
    ("/premium", ["show_premium"]),
    ("/sdate x", ["cmd_sdate"]),
    ("/language", ["cmd_language"]),
    (f"/dl {LINK}", ["private_dl"]),
    (LINK, ["handle_url"]),
    ("просто текст", ["handle_url"]),
    # неизвестная команда не уходит в скачивание как "текст со ссылкой"
    ("/nonsense", []),
    (f"/nonsense {LINK}", []),
    # в личке команды группового набора молчат
    (f"/sr_dl {LINK}", []),
    ("/sr_sdate x", []),
    ("/sr_help", []),
    ("/sr_stats", []),
    ("/sr_premium", []),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("text, expected", PRIVATE_ROUTES)
async def test_private_chat_routes(dp, bot, text, expected):
    assert await route(dp, bot, PRIVATE, text) == expected


GROUP_PREFIXED = [
    (f"/sr_dl {LINK}", ["group_download"]),
    ("/sr_sdate x", ["group_sdate"]),
    ("/sr_help", ["group_help"]),
    ("/sr_stats", ["group_stats"]),
    ("/sr_premium", ["group_premium"]),
]

GROUP_SILENT = [
    "/start", "/help", "/about", "/download", "/stats", "/premium", "/language",
    "/sdate x", f"/dl {LINK}", LINK, "просто текст", "", "/nonsense",
    f"смотрите {LINK}",
    # префикс есть, но команды с таким именем нет
    "/sr_start", "/sr_about", "/sr_language",
    # префикс не в начале команды - не наша
    f"/x_sr_dl {LINK}", f"sr_dl {LINK}",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("chat", [GROUP, SUPERGROUP], ids=["group", "supergroup"])
@pytest.mark.parametrize("text, expected", GROUP_PREFIXED)
async def test_group_takes_prefixed_commands(dp, bot, chat, text, expected):
    assert await route(dp, bot, chat, text) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("chat", [GROUP, SUPERGROUP], ids=["group", "supergroup"])
@pytest.mark.parametrize("text", GROUP_SILENT)
async def test_group_is_silent_without_prefix(dp, bot, chat, text):
    assert await route(dp, bot, chat, text or None) == []


@pytest.mark.asyncio
async def test_group_command_addressed_to_another_bot_is_ignored(dp, bot):
    assert await route(dp, bot, SUPERGROUP, "/sr_help@other_bot") == []
    assert await route(dp, bot, SUPERGROUP, f"/sr_dl@other_bot {LINK}") == []


@pytest.mark.asyncio
async def test_group_command_addressed_to_this_bot_is_taken(dp, bot):
    assert await route(dp, bot, SUPERGROUP, "/sr_help@soroka_bot") == ["group_help"]
    assert await route(dp, bot, SUPERGROUP, f"/sr_dl@soroka_bot {LINK}") == ["group_download"]


@pytest.mark.asyncio
async def test_group_prefix_is_case_sensitive(dp, bot):
    assert await route(dp, bot, SUPERGROUP, "/SR_help") == []


# ---------- служебные сообщения о переезде группы ----------


@pytest.mark.asyncio
async def test_migrate_to_service_message_goes_to_migration_handler(dp, bot):
    assert await route(dp, bot, GROUP, migrate_to_chat_id=-1000100) == ["on_migrate_to"]


@pytest.mark.asyncio
async def test_migrate_from_service_message_goes_to_migration_handler(dp, bot):
    assert await route(dp, bot, SUPERGROUP, migrate_from_chat_id=-100) == ["on_migrate_from"]


@pytest.mark.asyncio
async def test_migrate_service_message_in_private_chat_is_not_taken(dp, bot):
    assert await route(dp, bot, PRIVATE, migrate_to_chat_id=-1000100) == []


# ---------- кнопки (callback_query) ----------


async def press(dp, bot, chat, data):
    hits.clear()
    msg = Message(message_id=1, date=datetime.datetime(2026, 1, 1), chat=chat, text="меню")
    cb = CallbackQuery(
        id="1", chat_instance="x", data=data, message=msg,
        from_user=TgUser(id=1, is_bot=False, first_name="t"),
    )
    await dp.feed_update(bot, Update(update_id=1, callback_query=cb))
    return list(hits)


CALLBACKS = [
    # (data, в личке, в группе): у группы свои экраны для тех же кнопок
    ("stats", ["show_stats"], ["group_stats"]),
    ("premium", ["show_premium"], ["group_premium"]),
    ("help", ["cmd_help"], ["group_help"]),
    ("download_menu", ["download_menu"], ["group_help"]),
    # заявка на Premium общая: кнопка под предложением группы ведёт в те же обработчики
    ("premium_request", ["premium_request"], ["premium_request"]),
    ("premium_confirm", ["premium_confirm"], ["premium_confirm"]),
    ("cancel", ["cancel_premium"], ["cancel_premium"]),
    ("lang:en", ["choose_language"], ["choose_language"]),
    ("nonsense", [], []),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("data, private, _group", CALLBACKS)
async def test_callback_buttons_in_private_chat(dp, bot, data, private, _group):
    assert await press(dp, bot, PRIVATE, data) == private


@pytest.mark.asyncio
@pytest.mark.parametrize("chat", [GROUP, SUPERGROUP], ids=["group", "supergroup"])
@pytest.mark.parametrize("data, _private, group_hits", CALLBACKS)
async def test_callback_buttons_in_group(dp, bot, chat, data, _private, group_hits):
    assert await press(dp, bot, chat, data) == group_hits
