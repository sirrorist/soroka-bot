"""Steam Web API: сколько человек играл за последние две недели.

Ключ идёт в query-строке, поэтому ни URL запроса, ни текст исключений aiohttp
(они печатают URL) не попадают ни в лог, ни в ответ пользователю.
"""

import re

import aiohttp

from utils.i18n import t

API = "https://api.steampowered.com"
# communityvisibilitystate: 3 - профиль открыт, остальное - закрыт
PUBLIC = 3

# fullmatch по всей строке: хост - ровно steamcommunity.com, без чужого домена
# вокруг, логина и порта (находка tester 2026-09-30: search() брал имя из
# notsteamcommunity.com/id/... и из ?u=steamcommunity.com/id/...)
_PROFILE_URL = re.compile(
    r"(?:https?://)?(?:www\.)?steamcommunity\.com/(id|profiles)/([^/?#\s]+)/?(?:[/?#]\S*)?",
    re.I,
)
_STEAMID = re.compile(r"7656119\d{10}")
_VANITY = re.compile(r"[A-Za-z0-9_-]{2,32}")


class SteamError(Exception):
    """Ошибка, текст которой можно показать пользователю."""


def parse_profile(text: str) -> tuple[str, str] | None:
    """("steamid", "7656...") или ("vanity", "имя") из ссылки, SteamID64 или имени."""
    text = text.strip()
    # ссылка может стоять посреди текста: каждое слово с доменом проверяется целиком
    m = None
    for word in text.split():
        if "steamcommunity.com" in word.lower():
            m = _PROFILE_URL.fullmatch(word.strip(".,;:!?()<>\"'"))
            break
    if m:
        kind, value = m.group(1).lower(), m.group(2)
        if kind == "profiles":
            return ("steamid", value) if _STEAMID.fullmatch(value) else None
        return ("vanity", value) if _VANITY.fullmatch(value) else None
    if _STEAMID.fullmatch(text):
        return ("steamid", text)
    if _VANITY.fullmatch(text):
        return ("vanity", text)
    return None


async def _get(session: aiohttp.ClientSession, path: str, key: str, **params) -> dict:
    try:
        async with session.get(f"{API}/{path}", params={"key": key, **params}) as resp:
            if resp.status != 200:
                raise SteamError(t("steam.error_status", status=resp.status))
            return await resp.json(content_type=None)
    except SteamError:
        raise
    except (aiohttp.ClientError, TimeoutError, ValueError):
        # from None: в цепочке исключений остался бы URL с ключом
        raise SteamError(t("steam.error_timeout")) from None


async def recent_playtime(query: str, key: str) -> dict:
    """Имя, видимость профиля и игры за две недели: [(название, минуты)], по убыванию."""
    parsed = parse_profile(query)
    if not parsed:
        raise SteamError(t("steam.error_parse"))
    kind, value = parsed

    timeout = aiohttp.ClientTimeout(total=15)
    # trust_env: на машине разработки Steam доступен только через HTTPS_PROXY
    async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
        steamid = value
        if kind == "vanity":
            data = await _get(
                session, "ISteamUser/ResolveVanityURL/v1/", key, vanityurl=value
            )
            resp = data.get("response") or {}
            if resp.get("success") != 1 or not resp.get("steamid"):
                raise SteamError(t("steam.error_not_found"))
            steamid = resp["steamid"]

        data = await _get(
            session, "ISteamUser/GetPlayerSummaries/v2/", key, steamids=steamid
        )
        players = (data.get("response") or {}).get("players") or []
        if not players:
            raise SteamError(t("steam.error_not_found"))
        player = players[0]

        data = await _get(
            session, "IPlayerService/GetRecentlyPlayedGames/v1/", key, steamid=steamid
        )
        games = (data.get("response") or {}).get("games") or []

    played = sorted(
        (
            (
                g.get("name") or f"app {g.get('appid')}",
                int(g.get("playtime_2weeks") or 0),
            )
            for g in games
        ),
        key=lambda g: g[1],
        reverse=True,
    )
    return {
        "name": player.get("personaname") or steamid,
        "public": player.get("communityvisibilitystate") == PUBLIC,
        "games": [g for g in played if g[1] > 0],
    }
