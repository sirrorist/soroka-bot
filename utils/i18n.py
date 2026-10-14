"""Тексты бота: locales/<язык>.toml. Язык по умолчанию - русский, английский - если
человек выбрал его в /language (utils/lang_store.py).

Язык хранится в contextvar: LangMiddleware ставит его на каждый апдейт, а
asyncio.to_thread копирует контекст - поэтому t() работает и в загрузчиках,
которые крутятся в отдельном потоке.
"""

import tomllib
from contextvars import ContextVar
from pathlib import Path

LOCALES_DIR = Path(__file__).resolve().parent.parent / "locales"
DEFAULT_LANG = "ru"
# им проще по-русски, чем по-английски; остальным - английский
RU_LANGS = {"ru", "uk", "be", "kk"}

_lang: ContextVar[str] = ContextVar("lang", default=DEFAULT_LANG)


def _load() -> dict[str, dict]:
    return {
        p.stem: tomllib.loads(p.read_text("utf-8"))
        for p in sorted(LOCALES_DIR.glob("*.toml"))
    }


TEXTS = _load()
LANGS = tuple(TEXTS)


def lang_for(language_code: str | None) -> str:
    """Какой язык ближе по language_code из Telegram: ru для русскоязычных, иначе en.

    Язык ответов по нему не выбирается - только подсказка в /start."""
    code = (language_code or "").split("-")[0].lower()
    return "ru" if code in RU_LANGS else "en"


def set_lang(lang: str) -> None:
    _lang.set(lang if lang in TEXTS else DEFAULT_LANG)


def get_lang() -> str:
    return _lang.get()


def _lookup(lang: str, key: str):
    node = TEXTS.get(lang, {})
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def t(key: str, lang: str | None = None, **kwargs) -> str:
    """Текст по ключу "раздел.имя" на текущем языке; нет перевода - берётся русский."""
    text = _lookup(lang or get_lang(), key)
    if text is None:
        text = _lookup(DEFAULT_LANG, key)
    if not isinstance(text, str):
        raise KeyError(f"no text for {key!r}")
    return text.format(**kwargs) if kwargs else text
