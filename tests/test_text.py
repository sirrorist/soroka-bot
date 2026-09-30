"""utils/text.py: склонения, объёмы, шкала, короткое имя. Ожидания - литералы."""
import pytest

from utils.i18n import set_lang
from utils.text import bar, download_word, downloads_word, plural, short_name, size_parts, size_text

MB = 1024 * 1024
GB = 1024 * MB
E, F = "▱", "▰"


@pytest.mark.parametrize(
    "n, word",
    [
        (0, "many"), (1, "one"), (2, "few"), (3, "few"), (4, "few"), (5, "many"),
        (10, "many"), (11, "many"), (12, "many"), (13, "many"), (14, "many"),
        (15, "many"), (20, "many"), (21, "one"), (22, "few"), (24, "few"), (25, "many"),
        (100, "many"), (101, "one"), (102, "few"), (111, "many"), (112, "many"),
        (114, "many"), (121, "one"), (1000, "many"), (-1, "one"), (-2, "few"), (-11, "many"),
    ],
)
def test_plural_ru(n, word):
    assert plural(n, "one", "few", "many") == word


@pytest.mark.parametrize("n, word", [(0, "many"), (1, "one"), (-1, "one"), (2, "many"), (21, "many"), (101, "many")])
def test_plural_en_has_only_one_and_many(n, word):
    set_lang("en")
    assert plural(n, "one", "few", "many") == word


@pytest.mark.parametrize(
    "n, text",
    [(1, "1 скачивание"), (2, "2 скачивания"), (5, "5 скачиваний"), (11, "11 скачиваний"),
     (21, "21 скачивание"), (22, "22 скачивания"), (112, "112 скачиваний")],
)
def test_downloads_word_ru(n, text):
    assert downloads_word(n) == text
    assert text.endswith(download_word(n))


@pytest.mark.parametrize("n, text", [(0, "0 downloads"), (1, "1 download"), (2, "2 downloads"), (21, "21 downloads")])
def test_downloads_word_en(n, text):
    set_lang("en")
    assert downloads_word(n) == text


# ---------- size_parts / size_text ----------


@pytest.mark.parametrize(
    "size, expected",
    [
        (0, ("0", "МБ")),
        (int(1.5 * MB), ("1,5", "МБ")),
        (5 * MB, ("5,0", "МБ")),
        (10 * MB, ("10", "МБ")),
        (340 * MB, ("340", "МБ")),
        (350 * MB, ("350", "МБ")),
        (1023 * MB, ("1023", "МБ")),
        (1024 * MB, ("1", "ГБ")),
        (int(1.5 * GB), ("1,5", "ГБ")),
        (int(1.2 * GB), ("1,2", "ГБ")),
        (5 * GB, ("5", "ГБ")),  # целые гигабайты без ",0"
        (5120 * MB, ("5", "ГБ")),
        (10 * GB, ("10", "ГБ")),
    ],
)
def test_size_parts_ru(size, expected):
    assert size_parts(size) == expected


@pytest.mark.parametrize(
    "size, expected",
    [
        (int(1.5 * MB), ("1.5", "MB")),
        (340 * MB, ("340", "MB")),
        (5 * GB, ("5", "GB")),
        (int(1.5 * GB), ("1.5", "GB")),
    ],
)
def test_size_parts_en_uses_dot_and_english_units(size, expected):
    set_lang("en")
    assert size_parts(size) == expected


def test_size_text_joins_number_and_unit():
    assert size_text(350 * MB) == "350 МБ"
    assert size_text(int(1.5 * GB)) == "1,5 ГБ"
    assert size_text(5120 * MB) == "5 ГБ"
    set_lang("en")
    assert size_text(int(1.5 * GB)) == "1.5 GB"
    assert size_text(350 * MB) == "350 MB"


# ---------- bar ----------


def test_bar_empty():
    assert bar(0, 10) == E * 10


def test_bar_partial():
    assert bar(3, 10) == F * 3 + E * 7
    assert bar(5, 10) == F * 5 + E * 5


def test_bar_full():
    assert bar(10, 10) == F * 10


def test_bar_over_limit_is_clamped_to_full():
    assert bar(15, 10) == F * 10
    assert bar(10**9, 10) == F * 10


@pytest.mark.parametrize("used", [0, 1, 5, 10, 999])
@pytest.mark.parametrize("limit", [0, -3])
def test_bar_without_limit_is_empty_not_a_division_error(used, limit):
    assert bar(used, limit) == E * 10


def test_bar_shows_at_least_one_cell_when_something_is_used():
    # 1 из 1000 округляется в 0 клеток - но "что-то потрачено" должно быть видно
    assert bar(1, 1000) == F + E * 9
    assert bar(0, 1000) == E * 10  # а если ничего - ни одной


def test_bar_rounds_to_nearest_cell():
    assert bar(2, 3) == F * 7 + E * 3


def test_bar_custom_width_and_constant_length():
    assert bar(1, 2, width=4) == F * 2 + E * 2
    for used in range(0, 25):
        assert len(bar(used, 20)) == 10


# ---------- short_name ----------


@pytest.mark.parametrize("name", [None, "", "   ", "\n\t "])
def test_short_name_empty_gets_placeholder(name):
    assert short_name(name) == "Без имени"


def test_short_name_placeholder_in_english():
    set_lang("en")
    assert short_name(None) == "No name"


def test_short_name_keeps_short_name_and_collapses_whitespace():
    assert short_name("Аня") == "Аня"
    assert short_name("  Аня   Иванова \n") == "Аня Иванова"


def test_short_name_boundary_at_limit():
    assert short_name("a" * 30) == "a" * 30
    assert short_name("a" * 31) == "a" * 30 + "…"
    assert short_name("a" * 500) == "a" * 30 + "…"


def test_short_name_does_not_leave_space_before_ellipsis():
    assert short_name("a" * 29 + " bbb") == "a" * 29 + "…"


def test_short_name_custom_limit():
    assert short_name("abcdef", limit=3) == "abc…"
