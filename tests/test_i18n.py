"""utils/i18n.py и locales/*.toml: полнота переводов, плейсхолдеры, запасной язык.

Ключи, которые код передаёт в t("..."), находятся разбором исходников (ast), а не
списком в тесте: новый ключ без текста в locales/ роняет тест сам.
"""

import ast
import re
import string
import tomllib
from pathlib import Path

import pytest

from utils import i18n
from utils.i18n import LANGS, TEXTS, get_lang, lang_for, set_lang, t

ROOT = Path(__file__).resolve().parent.parent
# только по-русски: заявка администратору (владелец читает по-русски) и её части
RU_ONLY = {"premium.admin", "premium.target_self", "premium.target_chat"}
# подсказка "как переключить на английский" показывается только при русском языке
# (handlers/start.py), поэтому в en.toml она намеренно пустая
EMPTY_OK = {("en", "start.lang_hint")}
SKIP_DIRS = {
    "tests",
    "Docs",
    ".git",
    "__pycache__",
    ".ruff_cache",
    "storage",
    "temp",
    ".claude",
    "scripts",
}


def flatten(node, prefix=""):
    for key, value in node.items():
        if isinstance(value, dict):
            yield from flatten(value, f"{prefix}{key}.")
        else:
            yield f"{prefix}{key}", value


def flat(lang):
    return dict(flatten(TEXTS[lang]))


def placeholders(text):
    return {name for _, name, _, _ in string.Formatter().parse(text) if name}


def tags(text):
    return sorted(re.findall(r"</?[a-z]+", text))


def source_files():
    for path in ROOT.rglob("*.py"):
        if not (set(path.relative_to(ROOT).parts) & SKIP_DIRS):
            yield path


def t_calls():
    """Все вызовы t(...) в коде: (файл, строка, узел)."""
    for path in source_files():
        for node in ast.walk(ast.parse(path.read_text("utf-8"))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "t"
            ):
                yield path.relative_to(ROOT), node.lineno, node


# ---------- наборы ключей ----------


def test_languages_are_ru_and_en():
    assert sorted(LANGS) == ["en", "ru"]


def test_toml_files_on_disk_are_the_ones_loaded():
    on_disk = sorted(p.stem for p in (ROOT / "locales").glob("*.toml"))
    assert on_disk == sorted(TEXTS)
    for lang in on_disk:  # и читаются они как TOML, а не подменены в памяти
        assert (
            tomllib.loads((ROOT / "locales" / f"{lang}.toml").read_text("utf-8"))
            == TEXTS[lang]
        )


def test_key_sets_of_ru_and_en_match_except_ru_only_keys():
    ru, en = set(flat("ru")), set(flat("en"))
    assert ru - en == RU_ONLY
    assert en - ru == set()


def test_ru_only_keys_really_exist_in_ru():
    assert RU_ONLY <= set(flat("ru"))


@pytest.mark.parametrize("key", sorted(set(flat("ru")) - RU_ONLY))
def test_placeholders_match_between_languages(key):
    assert placeholders(flat("ru")[key]) == placeholders(flat("en")[key]), key


@pytest.mark.parametrize("key", sorted(set(flat("ru")) - RU_ONLY))
def test_html_tags_match_between_languages(key):
    # сломанная разметка в переводе - Telegram отвечает "can't parse entities"
    assert tags(flat("ru")[key]) == tags(flat("en")[key]), key


@pytest.mark.parametrize("lang", ["ru", "en"])
def test_no_empty_texts(lang):
    empty = [
        k
        for k, v in flat(lang).items()
        if (not isinstance(v, str) or not v.strip()) and (lang, k) not in EMPTY_OK
    ]
    assert empty == []


def test_language_hint_is_empty_only_where_it_is_never_shown():
    assert flat("ru")["start.lang_hint"].strip()
    assert flat("en")["start.lang_hint"] == ""


@pytest.mark.parametrize("lang", ["ru", "en"])
def test_every_text_formats_with_its_own_placeholders(lang):
    # {..} без пары или неэкранированная скобка выстрелят в бою исключением format
    for key, text in flat(lang).items():
        text.format(**dict.fromkeys(placeholders(text), "x"))


# ---------- ключи из кода ----------


def test_every_key_used_in_code_exists_in_both_languages():
    ru, en = flat("ru"), flat("en")
    seen, missing = 0, []
    for path, line, node in t_calls():
        arg = node.args[0]
        if not (isinstance(arg, ast.Constant) and isinstance(arg.value, str)):
            continue
        seen += 1
        key = arg.value
        used_ru_only = any(kw.arg == "lang" for kw in node.keywords)
        if key not in ru or (key not in en and key not in RU_ONLY):
            missing.append(f"{path}:{line} {key}")
        if key in RU_ONLY:
            # русские ключи обязаны вызываться с lang="ru" - иначе английский пользователь
            # получит русский текст (или владелец - английский)
            assert used_ru_only, f"{path}:{line} {key} без lang"
    assert seen > 80  # разбор действительно нашёл вызовы, а не молча ничего
    assert missing == []


def test_arguments_passed_to_t_match_placeholders_of_the_text():
    ru = flat("ru")
    problems = []
    for path, line, node in t_calls():
        arg = node.args[0]
        if (
            not (isinstance(arg, ast.Constant) and isinstance(arg.value, str))
            or arg.value not in ru
        ):
            continue
        if any(kw.arg is None for kw in node.keywords):
            continue  # **kwargs - состав неизвестен
        passed = {kw.arg for kw in node.keywords} - {"lang"}
        needed = placeholders(ru[arg.value])
        if passed != needed:
            problems.append(
                f"{path}:{line} {arg.value}: передано {sorted(passed)}, в тексте {sorted(needed)}"
            )
    assert problems == []


def test_menu_keys_built_dynamically_exist_and_fit_telegram_limits():
    from utils.menu import GROUP_COMMANDS, PRIVATE_COMMANDS

    for lang in ("ru", "en"):
        texts = flat(lang)
        keys = [f"menu.{n}" for n in PRIVATE_COMMANDS] + [
            f"menu.group_{n}" for n in GROUP_COMMANDS
        ]
        for key in keys:
            assert key in texts, (lang, key)
            assert 3 <= len(texts[key]) <= 256, (
                lang,
                key,
            )  # лимит описания команды в Bot API


# ---------- t() ----------


def test_t_default_language_is_russian():
    assert get_lang() == "ru"
    assert t("common.cancelled") == "Отменено"


def test_t_english_after_set_lang():
    set_lang("en")
    assert t("common.cancelled") == TEXTS["en"]["common"]["cancelled"]
    assert t("common.cancelled") != "Отменено"


def test_t_explicit_lang_overrides_current():
    set_lang("en")
    assert t("common.cancelled", lang="ru") == "Отменено"


def test_t_falls_back_to_russian_when_key_absent_in_english():
    # настоящий случай: этих ключей в en нет, и заявка администратору уходит по-русски
    assert "premium.target_self" not in flat("en")
    assert t("premium.target_self", lang="en") == "лично для себя"
    set_lang("en")
    assert t("premium.target_self") == "лично для себя"


def test_t_falls_back_to_russian_for_partial_english_table(monkeypatch):
    partial = {
        "en": {"only": {"here": "H"}},
        "ru": {"only": {"here": "Р", "there": "Т"}},
    }
    monkeypatch.setattr(i18n, "TEXTS", partial)
    assert t("only.here", lang="en") == "H"
    assert t("only.there", lang="en") == "Т"


def test_t_unknown_key_raises_keyerror_with_the_key():
    with pytest.raises(KeyError, match="no.such_key"):
        t("no.such_key")
    with pytest.raises(KeyError, match="common.no_such"):
        t("common.no_such")
    with pytest.raises(KeyError, match="common.cancelled.deeper"):
        t("common.cancelled.deeper")  # лист не раскрывается дальше


def test_t_unknown_key_raises_in_english_too():
    set_lang("en")
    with pytest.raises(KeyError):
        t("no.such_key")


def test_t_unknown_language_falls_back_to_russian_text():
    assert t("common.cancelled", lang="xx") == "Отменено"


def test_t_substitutes_arguments():
    assert t("download.too_big", size="120", max_mb=50) == (
        "Файл слишком большой: 120 МБ, а отправить можно до 50 МБ."
    )


def test_t_missing_argument_raises():
    with pytest.raises(KeyError):
        t("download.too_big", size="120")


def test_t_leaves_braces_alone_without_arguments():
    # без kwargs текст не форматируется: {max_mb} остаётся как есть
    assert "{max_mb}" in t("common.limits_block")


def test_set_lang_ignores_unknown_language():
    set_lang("en")
    set_lang("de")
    assert get_lang() == "ru"


# ---------- lang_for ----------


@pytest.mark.parametrize(
    "code, expected",
    [
        ("ru", "ru"),
        ("uk", "ru"),
        ("be", "ru"),
        ("kk", "ru"),
        ("ru-RU", "ru"),
        ("RU", "ru"),
        ("uk-UA", "ru"),
        ("en", "en"),
        ("en-US", "en"),
        ("de", "en"),
        ("zh-hans", "en"),
        (None, "en"),
        ("", "en"),
    ],
)
def test_lang_for(code, expected):
    assert lang_for(code) == expected
