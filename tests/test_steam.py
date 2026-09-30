"""/sdate: разбор профиля (utils/steam.py), формат ответа и команда (handlers/steam.py).

В сеть тесты не ходят: aiohttp-сессия подменена, ответы Steam - заготовки.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
from loguru import logger

from config import settings
from handlers import steam as steam_handler
from utils import steam as steam_utils
from utils.i18n import set_lang
from utils.steam import SteamError, parse_profile

ID64 = "76561197960287930"

# ---------- parse_profile ----------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("https://steamcommunity.com/id/gabelogannewell", ("vanity", "gabelogannewell")),
        ("https://steamcommunity.com/id/gabe/", ("vanity", "gabe")),
        ("http://steamcommunity.com/id/gabe", ("vanity", "gabe")),
        ("steamcommunity.com/id/gabe", ("vanity", "gabe")),
        ("https://www.steamcommunity.com/id/gabe", ("vanity", "gabe")),
        ("https://steamcommunity.com/id/gabe?xml=1", ("vanity", "gabe")),
        ("https://steamcommunity.com/id/gabe#top", ("vanity", "gabe")),
        ("HTTPS://SteamCommunity.com/ID/Gabe", ("vanity", "Gabe")),  # регистр имени сохраняется
        ("мой профиль https://steamcommunity.com/id/gabe/ - смотри", ("vanity", "gabe")),
        (f"https://steamcommunity.com/profiles/{ID64}", ("steamid", ID64)),
        (f"https://steamcommunity.com/profiles/{ID64}/", ("steamid", ID64)),
        (f"steamcommunity.com/profiles/{ID64}/games", ("steamid", ID64)),
        (ID64, ("steamid", ID64)),
        (f"  {ID64} \n", ("steamid", ID64)),
        ("gabe", ("vanity", "gabe")),
        ("  gabe  ", ("vanity", "gabe")),
        ("a_b-c9", ("vanity", "a_b-c9")),
        ("ab", ("vanity", "ab")),
        ("a" * 32, ("vanity", "a" * 32)),
    ],
)
def test_parse_profile_accepts(text, expected):
    assert parse_profile(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "a",  # короче двух символов
        "a" * 33,  # длиннее 32
        "две части",
        "габэ",  # имя в ссылке Steam - только латиница, цифры, _ и -
        "gab e",
        "???",
        "https://steamcommunity.com/id/",
        "https://steamcommunity.com/id/" + "a" * 40,
        "https://steamcommunity.com/id/gab%20e",
        "https://steamcommunity.com/profiles/123",  # не SteamID64
        "https://steamcommunity.com/profiles/gabe",  # в /profiles/ бывает только число
        f"https://steamcommunity.com/profiles/{ID64}9",  # 18 цифр
        "https://steamcommunity.com/groups/valve",
        "http://evil.example/id/gabe",
        "https://steamcommunity.com.evil.example/id/gabe",
        "https://steamcommunity.com:8080/id/gabe",
    ],
)
def test_parse_profile_rejects(text):
    assert parse_profile(text) is None


# был xfail(strict) - дефект исправлен 2026-09-30: _PROFILE_URL.fullmatch
@pytest.mark.parametrize(
    "text",
    [
        "https://notsteamcommunity.com/id/gabe",
        "https://evil.example/?u=steamcommunity.com/id/gabe",
        "https://user@steamcommunity.com/id/gabe",
    ],
)
def test_parse_profile_rejects_foreign_host_containing_steam_domain(text):
    assert parse_profile(text) is None


# ---------- recent_playtime / _get: без сети ----------


class FakeResponse:
    def __init__(self, status=200, payload=None):
        self.status = status
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self, content_type=None):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeSession:
    """aiohttp.ClientSession без сети: отвечает по последнему сегменту пути."""

    def __init__(self, routes, seen):
        self.routes = routes
        self.seen = seen

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def get(self, url, params=None):
        self.seen.append((url, params))
        for marker, response in self.routes.items():
            if marker in url:
                if isinstance(response, Exception):
                    raise response
                return response
        raise AssertionError(f"неожиданный запрос {url}")


@pytest.fixture
def steam_net(monkeypatch):
    seen = []
    routes = {}
    monkeypatch.setattr(
        steam_utils.aiohttp, "ClientSession", lambda **kw: FakeSession(routes, seen)
    )
    return SimpleNamespace(routes=routes, seen=seen)


def summaries(name="Bob", state=3):
    return FakeResponse(payload={"response": {"players": [{"personaname": name, "communityvisibilitystate": state}]}})


def recent(*games):
    return FakeResponse(payload={"response": {"games": list(games)}})


@pytest.mark.asyncio
async def test_recent_playtime_sorts_desc_drops_zero_and_names_unnamed_apps(steam_net):
    steam_net.routes.update({
        "GetPlayerSummaries": summaries(),
        "GetRecentlyPlayedGames": recent(
            {"name": "Мало", "playtime_2weeks": 5},
            {"name": "Много", "playtime_2weeks": 500},
            {"name": "Ноль", "playtime_2weeks": 0},
            {"appid": 42, "playtime_2weeks": 60},
            {"name": "Без времени"},
        ),
    })
    got = await steam_utils.recent_playtime(ID64, "KEY")
    assert got == {
        "name": "Bob",
        "public": True,
        "games": [("Много", 500), ("app 42", 60), ("Мало", 5)],
    }
    # с числовым SteamID64 имя не резолвится отдельным запросом
    assert not any("ResolveVanityURL" in url for url, _ in steam_net.seen)


@pytest.mark.asyncio
async def test_recent_playtime_private_profile_is_flagged_not_public(steam_net):
    steam_net.routes.update({"GetPlayerSummaries": summaries(state=1), "GetRecentlyPlayedGames": recent()})
    got = await steam_utils.recent_playtime(ID64, "KEY")
    assert got["public"] is False and got["games"] == []


@pytest.mark.asyncio
async def test_recent_playtime_resolves_vanity_name_first(steam_net):
    steam_net.routes.update({
        "ResolveVanityURL": FakeResponse(payload={"response": {"success": 1, "steamid": ID64}}),
        "GetPlayerSummaries": summaries(),
        "GetRecentlyPlayedGames": recent(),
    })
    await steam_utils.recent_playtime("gabe", "KEY")
    urls = [u for u, _ in steam_net.seen]
    assert "ResolveVanityURL" in urls[0]
    summary_params = [p for u, p in steam_net.seen if "GetPlayerSummaries" in u][0]
    assert summary_params["steamids"] == ID64


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"response": {"success": 42}},
        {"response": {"success": 42, "steamid": ID64}},  # не найден, даже если поле есть
        {"response": {"success": 1}},
        {"response": {}},
        {},
    ],
)
async def test_recent_playtime_unknown_vanity_name_is_not_found(steam_net, payload):
    steam_net.routes["ResolveVanityURL"] = FakeResponse(payload=payload)
    with pytest.raises(SteamError, match="Не нашёл такой профиль"):
        await steam_utils.recent_playtime("nobody", "KEY")


@pytest.mark.asyncio
async def test_recent_playtime_no_players_is_not_found(steam_net):
    steam_net.routes["GetPlayerSummaries"] = FakeResponse(payload={"response": {"players": []}})
    with pytest.raises(SteamError, match="Не нашёл такой профиль"):
        await steam_utils.recent_playtime(ID64, "KEY")


@pytest.mark.asyncio
async def test_recent_playtime_garbage_query_fails_before_any_request(steam_net):
    with pytest.raises(SteamError, match="Не понял, чей это профиль"):
        await steam_utils.recent_playtime("???", "KEY")
    assert steam_net.seen == []


@pytest.mark.asyncio
async def test_steam_http_error_status_is_reported_without_key(steam_net):
    steam_net.routes["GetPlayerSummaries"] = FakeResponse(status=403)
    with pytest.raises(SteamError) as err:
        await steam_utils.recent_playtime(ID64, "SECRETKEY")
    assert str(err.value) == "Steam не ответил (ошибка 403). Попробуйте позже."
    assert "SECRETKEY" not in str(err.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "boom",
    [aiohttp.ClientError("GET https://api.steampowered.com/?key=SECRETKEY"), TimeoutError("SECRETKEY"),
     ValueError("bad json SECRETKEY")],
)
async def test_steam_network_errors_become_generic_error_and_hide_the_key(steam_net, boom):
    steam_net.routes["GetPlayerSummaries"] = boom
    with pytest.raises(SteamError) as err:
        await steam_utils.recent_playtime(ID64, "SECRETKEY")
    assert str(err.value) == "Steam не ответил. Попробуйте позже."
    # цепочка исключений не тащит за собой URL с ключом
    assert err.value.__cause__ is None
    assert err.value.__suppress_context__ is True


@pytest.mark.asyncio
async def test_steam_bad_json_body_becomes_generic_error(steam_net):
    steam_net.routes["GetPlayerSummaries"] = FakeResponse(payload=ValueError("not json"))
    with pytest.raises(SteamError, match="Steam не ответил"):
        await steam_utils.recent_playtime(ID64, "K")


# ---------- format_playtime ----------


def stats(name="Bob", public=True, games=()):
    return {"name": name, "public": public, "games": list(games)}


def test_format_closed_profile():
    assert steam_handler.format_playtime(stats(public=False)) == (
        "🔒 <b>Bob</b>: профиль закрыт - Steam не показывает его игры."
    )


def test_format_public_profile_without_games():
    assert steam_handler.format_playtime(stats(public=True)) == (
        "🎮 <b>Bob</b>: за две недели игр нет - или в Steam не заходили, "
        "или список игр скрыт в настройках приватности."
    )


def test_format_games_with_totals_and_time_format():
    text = steam_handler.format_playtime(stats(games=[("Dota 2", 185), ("CS2", 45)]))
    assert text == (
        "<b>🎮 Bob</b>\n<b>3 ч 50 мин</b> в Steam за две недели\n\n"
        "<blockquote>Dota 2 - 3 ч 5 мин\nCS2 - 45 мин</blockquote>"
    )


def test_format_exactly_ten_games_has_no_more_line():
    games = [(f"G{i}", 100 - i) for i in range(10)]
    text = steam_handler.format_playtime(stats(games=games))
    assert "и ещё" not in text
    assert text.count("\nG") + text.count(">G") == 10


def test_format_more_than_ten_games_lists_ten_but_totals_all():
    games = [(f"G{i}", 60) for i in range(25)]
    text = steam_handler.format_playtime(stats(games=games))
    assert "<b>25 ч 0 мин</b>" in text  # итог - по всем 25 играм
    assert "G9 - 1 ч 0 мин" in text and "G10 -" not in text
    assert text.endswith("<i>и ещё игр: 15</i>")


def test_format_eleventh_game_gives_one_more():
    text = steam_handler.format_playtime(stats(games=[(f"G{i}", 10) for i in range(11)]))
    assert text.endswith("<i>и ещё игр: 1</i>")


def test_format_escapes_name_and_titles_in_every_branch():
    evil = "<b>x</b> & <script>"
    esc = "&lt;b&gt;x&lt;/b&gt; &amp; &lt;script&gt;"
    assert f"<b>{esc}</b>" in steam_handler.format_playtime(stats(name=evil, public=False))
    assert f"<b>{esc}</b>" in steam_handler.format_playtime(stats(name=evil, public=True))
    text = steam_handler.format_playtime(stats(name=evil, games=[(evil, 60)]))
    assert f"🎮 {esc}</b>" in text
    assert f"{esc} - 1 ч 0 мин" in text
    assert "<script>" not in text


def test_format_english_units():
    set_lang("en")
    text = steam_handler.format_playtime(stats(games=[("Dota 2", 185)]))
    assert "Dota 2 - 3 h 5 min" in text


# ---------- /sdate: команда ----------


@pytest.fixture
def sdate_env(monkeypatch):
    monkeypatch.setattr(settings, "steam_api_key", "KEY")
    monkeypatch.setattr(steam_handler, "_last_call", {}, raising=False)
    monkeypatch.setattr(steam_handler, "_steam_slots", asyncio.Semaphore(3), raising=False)
    recent_playtime = AsyncMock(return_value=stats(games=[("Dota 2", 60)]))
    monkeypatch.setattr(steam_handler, "recent_playtime", recent_playtime)
    message = MagicMock()
    message.answer = AsyncMock()
    message.from_user = SimpleNamespace(id=5)
    message.chat = SimpleNamespace(id=5)
    return SimpleNamespace(message=message, recent=recent_playtime)


def cmd(args, name="sdate"):
    return SimpleNamespace(args=args, command=name)


@pytest.mark.asyncio
async def test_sdate_without_argument_shows_usage_with_command_name(sdate_env):
    await steam_handler.cmd_sdate(sdate_env.message, cmd(None, "sr_sdate"))
    assert "<code>/sr_sdate профиль</code>" in sdate_env.message.answer.await_args.args[0]
    sdate_env.recent.assert_not_awaited()


@pytest.mark.asyncio
async def test_sdate_without_api_key_says_unavailable(sdate_env, monkeypatch):
    monkeypatch.setattr(settings, "steam_api_key", None)
    await steam_handler.cmd_sdate(sdate_env.message, cmd("gabe"))
    sdate_env.message.answer.assert_awaited_once_with("Эта команда сейчас недоступна.")
    sdate_env.recent.assert_not_awaited()


@pytest.mark.asyncio
async def test_sdate_success_passes_query_and_key(sdate_env):
    await steam_handler.cmd_sdate(sdate_env.message, cmd("gabe"))
    sdate_env.recent.assert_awaited_once_with("gabe", "KEY")
    assert sdate_env.message.answer.await_args.args[0].startswith("<b>🎮 Bob</b>")


@pytest.mark.asyncio
async def test_sdate_steam_error_text_is_escaped(sdate_env):
    sdate_env.recent.side_effect = SteamError("плохо <b>x</b> & y")
    await steam_handler.cmd_sdate(sdate_env.message, cmd("gabe"))
    sdate_env.message.answer.assert_awaited_once_with("плохо &lt;b&gt;x&lt;/b&gt; &amp; y")


@pytest.mark.asyncio
async def test_sdate_unexpected_error_gives_generic_text_and_logs_only_type(sdate_env):
    sdate_env.recent.side_effect = RuntimeError("GET https://api.steampowered.com/?key=SECRETKEY")
    lines = []
    sink = logger.add(lambda m: lines.append(m.record["message"]), level="INFO")
    try:
        await steam_handler.cmd_sdate(sdate_env.message, cmd("gabe"))
    finally:
        logger.remove(sink)
    sdate_env.message.answer.assert_awaited_once_with("Не получилось узнать. Попробуйте позже.")
    assert lines == ["/sdate failed: RuntimeError"]


@pytest.mark.asyncio
async def test_sdate_repeat_within_cooldown_does_not_reach_steam(sdate_env, monkeypatch):
    clock = SimpleNamespace(now=1000.0, monotonic=lambda: clock.now)
    monkeypatch.setattr(steam_handler, "time", clock)
    await steam_handler.cmd_sdate(sdate_env.message, cmd("gabe"))
    clock.now += steam_handler.COOLDOWN_S - 1
    await steam_handler.cmd_sdate(sdate_env.message, cmd("gabe"))
    assert sdate_env.recent.await_count == 1
    assert "Не так часто" in sdate_env.message.answer.await_args.args[0]
    clock.now += 1
    await steam_handler.cmd_sdate(sdate_env.message, cmd("gabe"))
    assert sdate_env.recent.await_count == 2
