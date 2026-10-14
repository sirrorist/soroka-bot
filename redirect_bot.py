"""Старый бот после переезда: на любое сообщение отвечает, что бот переехал.

Один файл без базы и без остального кода - слово владельца 2026-09-30. Юзернейм
бота в Telegram не меняется, поэтому старый @tiktubeDownloadBot остаётся жить
заглушкой, а Soroka работает под новым токеном.

Запуск: сервис redirect в docker-compose.yml (тот же образ) или
    OLD_BOT_TOKEN=... NEW_BOT_USERNAME=... python redirect_bot.py
Переменные:
    OLD_BOT_TOKEN     - токен старого бота (обязательно)
    NEW_BOT_USERNAME  - юзернейм нового бота без @ (обязательно)
    MOVED_PHOTO       - картинка к сообщению (баннер, скриншот); нет файла - только текст

Режим - long polling: webhook старого бота снимается при старте, traefik заглушке не
нужен. Только стандартная библиотека, без aiogram и asyncio: память - около 25 МБ
против ~170 МБ с aiogram (замер 2026-10-01). Ответы идут по одному - заглушке
параллельность не нужна.
"""

import http.client
import json
import logging
import os
import re
import signal
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

TOKEN = os.environ["OLD_BOT_TOKEN"].strip()
# токен идёт в путь запроса: с пробелом или переводом строки http.client упал бы
# с InvalidURL, а в её тексте - путь целиком, то есть токен попал бы в лог
if not re.fullmatch(r"\d+:[A-Za-z0-9_-]+", TOKEN):
    raise SystemExit("OLD_BOT_TOKEN: не похоже на токен бота (значение не печатается)")
NEW = os.environ["NEW_BOT_USERNAME"].lstrip("@")
PHOTO = Path(os.environ.get("MOVED_PHOTO", "moved.png"))
LINK = f"https://t.me/{NEW}"
API = "https://api.telegram.org"

# Тексты - здесь, а не в locales/: заглушка живёт одним файлом
TEXTS = {
    "ru": {
        "moved": (
            "🐦 <b>Бот переехал!</b>\n\n"
            f"Теперь он называется <b>Soroka</b> и живёт здесь: @{NEW}\n\n"
            "Всё как раньше: пришлите ссылку на ролик из TikTok, YouTube, Instagram или X - "
            "и получите файл. Ваша статистика и Premium переехали вместе с ботом.\n\n"
            "Этот бот больше ничего не скачивает."
        ),
        "button": "Открыть Soroka",
        "about": f"Бот переехал: @{NEW}",
        "description": (
            f"Этот бот переехал и больше не скачивает. Новый адрес - @{NEW}: "
            "всё то же самое, статистика и Premium сохранились."
        ),
    },
    "en": {
        "moved": (
            "🐦 <b>The bot has moved!</b>\n\n"
            f"It's now called <b>Soroka</b> and lives here: @{NEW}\n\n"
            "Everything works as before: send a link to a video from TikTok, YouTube, "
            "Instagram or X and get the file. Your stats and Premium moved too.\n\n"
            "This bot no longer downloads anything."
        ),
        "button": "Open Soroka",
        "about": f"The bot has moved: @{NEW}",
        "description": (
            f"This bot has moved and no longer downloads. The new address is @{NEW}: "
            "same features, your stats and Premium are kept."
        ),
    },
}
RU_LANGS = {"ru", "uk", "be", "kk"}
# в группе - не чаще раза в час на чат, в личке - раза в минуту на человека
COOLDOWN_S = {"group": 3600, "private": 60}
# запись старше самого длинного окна ни на что не влияет - раз в час такие убираются,
# иначе _last растёт на запись с каждым новым чатом
PRUNE_EVERY_S = 3600
POLL_TIMEOUT_S = 30
MAX_BACKOFF_S = 60
# фото больше этого Telegram не примет - тогда только текст
PHOTO_MAX_BYTES = 10 * 1024 * 1024
# сбои связи: OSError (URLError, таймаут, сброс), обрыв посреди ответа
# (http.client.IncompleteRead и родня - не OSError), битый JSON
NET_ERRORS = (OSError, ValueError, http.client.HTTPException)

_last: dict[int, float] = {}
_pruned_at = 0.0
# картинка заливается один раз, дальше уходит по file_id из ответа Telegram
_photo_id: str | None = None

log = logging.getLogger("redirect")


class ApiError(Exception):
    """Ответ Bot API с ok=false. Текст - описание от Telegram, без адреса с токеном."""

    def __init__(self, code: int | None, description: str, retry_after: int | None):
        super().__init__(f"{code}: {description}")
        self.code = code
        self.description = description
        self.retry_after = retry_after


def _multipart(fields: dict, file_field: str, path: Path):
    """Тело multipart/form-data кусками: файл не держится в памяти целиком.

    Возвращает (итератор байтов, длина, Content-Type); длина нужна urllib для
    итерируемого тела.
    """
    boundary = os.urandom(16).hex()
    head = []
    for name, value in fields.items():
        if not isinstance(value, str):
            value = (
                json.dumps(value)
                if isinstance(value, dict | list | bool)
                else str(value)
            )
        # charset явно - как у aiohttp: без него кириллицу в подписи можно прочесть
        # не как UTF-8
        head.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n'
            f"Content-Type: text/plain; charset=utf-8\r\n\r\n{value}\r\n".encode()
        )
    head.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; '
        f'filename="{path.name}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode()
    )
    head = b"".join(head)
    tail = f"\r\n--{boundary}--\r\n".encode()
    size = len(head) + path.stat().st_size + len(tail)

    def chunks():
        yield head
        with path.open("rb") as f:
            while chunk := f.read(64 * 1024):
                yield chunk
        yield tail

    return chunks(), size, f"multipart/form-data; boundary={boundary}"


def _api(
    method: str,
    fields: dict | None = None,
    photo: Path | None = None,
    timeout: float = 15,
):
    """Вызов Bot API. Сбой связи - одно из NET_ERRORS, отказ - ApiError."""
    fields = {k: v for k, v in (fields or {}).items() if v is not None}
    if photo is not None:
        body, size, ctype = _multipart(fields, "photo", photo)
        headers = {"Content-Type": ctype, "Content-Length": str(size)}
    else:
        body = json.dumps(fields).encode()
        headers = {"Content-Type": "application/json"}
    request = urllib.request.Request(
        f"{API}/bot{TOKEN}/{method}", data=body, headers=headers
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.load(response)
    except urllib.error.HTTPError as e:
        # Bot API отвечает на отказ 4xx с телом {"ok": false, ...}
        with e:
            try:
                data = json.load(e)
            except ValueError:
                raise ApiError(e.code, "no JSON in response", None) from None
    if not isinstance(data, dict):
        raise ApiError(None, "unexpected response", None)
    if not data.get("ok"):
        raise ApiError(
            data.get("error_code"),
            data.get("description", ""),
            (data.get("parameters") or {}).get("retry_after"),
        )
    return data["result"]


def _lang(code: str | None) -> str:
    return "ru" if (code or "").split("-")[0].lower() in RU_LANGS else "en"


def _keyboard(lang: str) -> dict:
    return {"inline_keyboard": [[{"text": TEXTS[lang]["button"], "url": LINK}]]}


def _due(chat_id: int, kind: str) -> bool:
    """Не отвечать чаще COOLDOWN_S: иначе в активной группе бот засорит чат."""
    global _pruned_at
    now = time.monotonic()
    if now - _pruned_at >= PRUNE_EVERY_S:
        longest = max(COOLDOWN_S.values())
        for key in [k for k, t in _last.items() if now - t >= longest]:
            del _last[key]
        _pruned_at = now
    if now - _last.get(chat_id, -COOLDOWN_S[kind]) < COOLDOWN_S[kind]:
        return False
    _last[chat_id] = now
    return True


def _photo_ok() -> bool:
    try:
        return PHOTO.is_file() and PHOTO.stat().st_size <= PHOTO_MAX_BYTES
    except OSError:
        return False


def _send_moved(chat_id: int, lang: str, thread_id: int | None = None) -> None:
    global _photo_id
    fields = {
        "chat_id": chat_id,
        # в топике супергруппы - ответ в тот же топик
        "message_thread_id": thread_id,
        "parse_mode": "HTML",
        "reply_markup": _keyboard(lang),
    }
    if not _photo_ok():
        _api(
            "sendMessage",
            fields
            | {
                "text": TEXTS[lang]["moved"],
                "link_preview_options": {"is_disabled": True},
            },
        )
        return
    fields["caption"] = TEXTS[lang]["moved"]
    if _photo_id:
        try:
            _api("sendPhoto", fields | {"photo": _photo_id})
            return
        except ApiError as e:
            # 400 бывает и от чата (закрытый топик, нет права слать фото) - тогда
            # перезаливка не поможет; заново - только если отказ про сам файл
            if e.code != 400 or "file" not in e.description.lower():
                raise
            _photo_id = None
    result = _api("sendPhoto", fields, photo=PHOTO)
    _photo_id = result["photo"][-1]["file_id"]


def handle(update: dict) -> None:
    callback = update.get("callback_query")
    if callback:
        # кнопки под старыми сообщениями: всплывающее уведомление со ссылкой
        lang = _lang(callback["from"].get("language_code"))
        _api(
            "answerCallbackQuery",
            {
                "callback_query_id": callback["id"],
                "text": TEXTS[lang]["about"],
                "show_alert": True,
            },
        )
        return
    message = update.get("message")
    if not message:
        return
    chat = message["chat"]
    if chat["type"] == "private":
        kind = "private"
    elif (message.get("text") or "").startswith("/"):
        # в группе - только на команды, как и раньше, раз в час на чат
        kind = "group"
    else:
        return
    if _due(chat["id"], kind):
        thread = (
            message.get("message_thread_id")
            if message.get("is_topic_message")
            else None
        )
        _send_moved(
            chat["id"], _lang((message.get("from") or {}).get("language_code")), thread
        )


def _setup_profile() -> None:
    """Профиль старого бота тоже говорит о переезде; меню команд - пустое."""
    for lang, code in (("en", None), ("ru", "ru")):
        _api(
            "setMyShortDescription",
            {"short_description": TEXTS[lang]["about"], "language_code": code},
        )
        _api(
            "setMyDescription",
            {"description": TEXTS[lang]["description"], "language_code": code},
        )
        _api("deleteMyCommands", {"language_code": code})


def poll() -> None:
    offset = None
    backoff = 1
    while True:
        try:
            updates = _api(
                "getUpdates",
                {
                    "offset": offset,
                    "timeout": POLL_TIMEOUT_S,
                    "allowed_updates": ["message", "callback_query"],
                },
                timeout=POLL_TIMEOUT_S + 10,
            )
            backoff = 1
        except ApiError as e:
            if e.code in (401, 404):
                raise  # токен неверный: повторять бессмысленно, пусть падает в лог
            log.warning("getUpdates refused: %s", e)
            time.sleep(e.retry_after or backoff)
            backoff = min(backoff * 2, MAX_BACKOFF_S)
            continue
        except NET_ERRORS as e:
            log.warning("getUpdates failed: %s", type(e).__name__)
            time.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF_S)
            continue
        for update in updates:
            try:
                # смещение - до обработки: апдейт, на котором ответ упал, не
                # повторяется - ни в этом процессе, ни после перезапуска
                offset = update["update_id"] + 1
                handle(update)
            except ApiError as e:
                # 403 - бота заблокировали или выгнали из чата: молча дальше
                if e.code != 403:
                    log.warning("reply refused: %s", e)
                if e.retry_after:
                    time.sleep(e.retry_after)
            except Exception as e:
                # апдейт неожиданной формы не должен ронять цикл: иначе после
                # перезапуска тот же неподтверждённый апдейт уронит его снова
                log.warning("reply failed: %s", type(e).__name__)


def _stop(signum, frame) -> None:
    sys.exit(0)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s"
    )
    # в контейнере это PID 1: без своего обработчика SIGTERM игнорируется, и
    # docker stop ждёт 10 с до SIGKILL
    signal.signal(signal.SIGTERM, _stop)
    # старый бот работал через webhook - без его снятия polling не получит апдейтов
    _api("deleteWebhook", {"drop_pending_updates": False})
    try:
        _setup_profile()
    except (ApiError, *NET_ERRORS) as e:
        log.warning("Profile not updated: %s", type(e).__name__)
    log.info("Redirect bot started, pointing to @%s", NEW)
    poll()


if __name__ == "__main__":
    main()
