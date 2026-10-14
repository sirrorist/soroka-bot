"""Выбранный пользователем язык: таблица user_settings и кэш в памяти.

По умолчанию бот говорит по-русски (слово владельца 2026-09-30), английский - если
человек выбрал его в /language. Кэш - чтобы не ходить в базу на каждый апдейт;
процесс бота один, поэтому кэш не расходится с базой.
"""

from database import UserSetting, session_scope
from utils.i18n import DEFAULT_LANG, TEXTS

_cache: dict[int, str] = {}
# LangMiddleware зовёт get_user_lang на каждый апдейт, в том числе от чужих людей в
# группах; без предела кэш рос бы всю жизнь процесса (находка security-reviewer)
CACHE_MAX = 50_000


async def get_user_lang(user_id: int) -> str:
    """Язык пользователя: сохранённый выбор или язык по умолчанию."""
    if user_id in _cache:
        return _cache[user_id]
    lang = DEFAULT_LANG
    async with session_scope() as session:
        setting = await session.get(UserSetting, user_id)
        if setting and setting.lang in TEXTS:
            lang = setting.lang
    if len(_cache) >= CACHE_MAX:
        _cache.clear()
    _cache[user_id] = lang
    return lang


async def save_user_lang(user_id: int, lang: str) -> None:
    """Запомнить выбор. merge - вставит строку или обновит существующую."""
    async with session_scope() as session:
        await session.merge(UserSetting(user_id=user_id, lang=lang))
        await session.commit()
    _cache[user_id] = lang
