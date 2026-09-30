"""Общее для загрузчиков: разбор ссылки, ограничения yt-dlp, каталог на запрос."""
import re
import shutil
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

from loguru import logger
from yt_dlp.utils import match_filter_func

from config import settings
from utils.i18n import t

# Хост ссылки должен совпасть с одним из этих точно или по суффиксу ".<хост>":
# подстрока не годится - "x.com" есть в "linux.com", "tiktok.com" - в любом query.
PLATFORM_HOSTS = {
    "tiktok": ("tiktok.com",),
    "youtube": ("youtube.com", "youtu.be"),
    "instagram": ("instagram.com",),
    "twitter": ("twitter.com", "x.com"),
    "reddit": ("reddit.com",),
    "pinterest": ("pinterest.com",),
}

# Экстракторы yt-dlp по площадкам: только одиночные ролики. Без generic, который
# скачивает с любого адреса, и без плейлистов, профилей и поиска.
ALLOWED_EXTRACTORS = {
    "tiktok": ["tiktok", "vm.tiktok"],
    "youtube": ["youtube"],
    "instagram": ["instagram"],
    "twitter": ["twitter"],
}

MAX_DURATION_S = 20 * 60
# Больше Bot API не отправит: качать такой файл - зря тратить трафик и слот
TELEGRAM_MAX_MB = 50

_TRAILING_PUNCT = ".,;:!?)]}>'\""
_HOST_RE = re.compile(r"[a-z0-9.-]+")


class DownloadLimitError(Exception):
    """Скачивание прервано ограничением бота: размер или время."""


def extract_url(text: str) -> tuple[str, str] | None:
    """Первая ссылка на известную площадку в тексте: (площадка, URL) или None."""
    for token in text.split():
        token = token.lstrip("<(\"'").rstrip(_TRAILING_PUNCT)
        candidate = token if "://" in token else f"https://{token}"
        try:
            parts = urlsplit(candidate)
            port = parts.port
        except ValueError:
            continue
        if parts.scheme.lower() not in ("http", "https"):
            continue
        # порт и user@host в ссылках площадок не бывают, зато ими обманывают проверку
        if port is not None or parts.username or parts.password:
            continue
        host = (parts.hostname or "").lower().rstrip(".")
        # urlsplit оставляет в имени хоста "\" и %-последовательности: другой
        # разборщик прочитает такой адрес иначе, чем эта проверка
        if not _HOST_RE.fullmatch(host):
            continue
        for platform, hosts in PLATFORM_HOSTS.items():
            if any(host == h or host.endswith("." + h) for h in hosts):
                return platform, parts.geturl()
    return None


def new_workdir(platform: str) -> Path:
    """Свой каталог на каждый запрос: чужой файл с тем же id не подсунуть."""
    base = Path(settings.temp_dir)
    base.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=f"{platform}_", dir=base))


def remove_workdir(path: str | Path) -> None:
    """Удалить каталог запроса. Трогает только прямые подкаталоги temp_dir."""
    p = Path(path)
    workdir = p if p.is_dir() else p.parent
    if workdir.resolve().parent == Path(settings.temp_dir).resolve():
        shutil.rmtree(workdir, ignore_errors=True)


def find_file(workdir: Path) -> Path | None:
    """Готовый файл в каталоге запроса. Имя после постобработки заранее не известно."""
    files = [
        f
        for f in workdir.iterdir()
        if f.is_file() and not f.name.endswith((".part", ".ytdl"))
    ]
    return max(files, key=lambda f: f.stat().st_size, default=None)


def ydl_limits(workdir: Path, platform: str, max_mb: int | None = None) -> dict:
    """Параметры yt-dlp, которые ограничивают один запрос по адресу, размеру и времени."""
    max_bytes = min(max_mb or settings.max_file_size_mb, TELEGRAM_MAX_MB) * 1024 * 1024
    deadline = time.monotonic() + settings.download_timeout_s

    # max_filesize yt-dlp сверяет только с Content-Length; поток без длины
    # и медленную отдачу обрывает этот хук
    def guard(status: dict) -> None:
        if (status.get("downloaded_bytes") or 0) > max_bytes:
            raise DownloadLimitError(t("errors.too_big", mb=max_bytes // (1024 * 1024)))
        if time.monotonic() > deadline:
            raise DownloadLimitError(t("errors.too_slow"))

    return {
        "outtmpl": str(workdir / f"{platform}_%(id)s.%(ext)s"),
        "allowed_extractors": [re.escape(n) for n in ALLOWED_EXTRACTORS[platform]],
        "noplaylist": True,
        "playlist_items": "1",
        "max_filesize": max_bytes,
        "socket_timeout": 30,
        "match_filter": match_filter_func(f"!is_live & duration <=? {MAX_DURATION_S}"),
        "progress_hooks": [guard],
        # полоса прогресса иначе пишет в stdout сотни строк на ролик; хуки работают и так
        "noprogress": True,
    }


def max_send_mb() -> int:
    """Сколько МБ бот реально отправит: меньшее из настройки и предела Bot API."""
    return min(settings.max_file_size_mb, TELEGRAM_MAX_MB)


def short_error(e: Exception) -> str:
    """Текст ошибки для пользователя: без трассировки и не длиннее 200 символов."""
    for cause in (e, e.__cause__, e.__context__):
        if isinstance(cause, DownloadLimitError):
            return str(cause)
    return str(e).removeprefix("ERROR: ")[:200]


def user_reason(e: Exception, fallback: str, platform: str) -> str:
    """Причина для пользователя: наша (лимит размера и срока) - как есть, текст yt-dlp -
    только в лог. Он английский и о внутренностях, пользователю не помогает."""
    for cause in (e, e.__cause__, e.__context__):
        if isinstance(cause, DownloadLimitError):
            return str(cause)
    logger.info(f"{platform} download error: {short_error(e)}")
    return fallback
