"""Мелочи для текстов бота. Слова и единицы - из locales/, на текущем языке."""
from utils.i18n import get_lang, t


def plural(n: int, one: str, few: str, many: str) -> str:
    """Слово в нужной форме: 1 скачивание, 2 скачивания, 5 скачиваний."""
    if get_lang() != "ru":
        return one if abs(n) == 1 else many
    n = abs(n)
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def download_word(n: int) -> str:
    """Только слово "скачивание" в нужной форме - когда число выделяется отдельно."""
    return plural(n, t("units.download_one"), t("units.download_few"), t("units.download_many"))


def downloads_word(n: int) -> str:
    """Число со словом "скачивание" в нужной форме."""
    return f"{n} {download_word(n)}"


def size_parts(size_bytes: int) -> tuple[str, str]:
    """Объём числом и единицей отдельно: ("340", "МБ") или ("1,2", "ГБ")."""
    sep = "," if get_lang() == "ru" else "."
    mb = size_bytes / (1024 * 1024)
    # сравнивать то, что будет показано после округления, а не сырое число
    if round(mb) < 1024:
        if size_bytes <= 0:
            return "0", t("units.mb")
        num = f"{mb:.0f}" if round(mb, 1) >= 10 else f"{mb:.1f}".replace(".", sep)
        return num, t("units.mb")
    gb = round(mb / 1024, 1)
    num = f"{gb:.0f}" if gb == int(gb) else f"{gb:.1f}".replace(".", sep)
    return num, t("units.gb")


def size_text(size_bytes: int) -> str:
    """Объём по-человечески: 340 МБ или 1,2 ГБ."""
    return " ".join(size_parts(size_bytes))


def bar(used: int, limit: int, width: int = 10) -> str:
    """Шкала расхода лимита: ▰▰▰▱▱▱▱▱▱▱. Хоть что-то потрачено - хотя бы одна клетка."""
    if limit <= 0:
        return "▱" * width
    filled = round(width * min(used, limit) / limit)
    if used > 0:
        filled = max(filled, 1)
    return "▰" * filled + "▱" * (width - filled)


def short_name(name: str | None, limit: int = 30) -> str:
    """Имя для текста: пустое - "Без имени", длинное - обрезано. Экранирует вызывающий."""
    name = " ".join((name or "").split())
    if not name:
        return t("common.no_name")
    return name if len(name) <= limit else name[:limit].rstrip() + "…"
