"""Run with python -m sobriety; a different token from Soroka is required."""

import logging
import signal
import time
from datetime import datetime

from .api import ApiError, NetworkError, Telegram
from .bot import Bot
from .clock import UTC
from .diagnostics import configure_logging, operation
from .settings import Settings
from .store import Store

log = logging.getLogger("sobriety")


def main():
    settings = Settings.from_env()
    configure_logging(settings)
    log.info("Запуск счётчика; хранение логов: текущий и два предыдущих дня UTC")
    running = True

    def stop(signum, frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    api = Telegram(settings.token)
    with_store = Store(settings.db_path)
    try:
        identity = api.call("getMe")
        if (
            settings.expected_username
            and identity["username"].casefold() != settings.expected_username.casefold()
        ):
            raise SystemExit(
                "Токен принадлежит другому боту. Запуск остановлен без изменения его настроек."
            )
        # Never remove an existing webhook automatically: this may be the wrong token.
        webhook = api.call("getWebhookInfo")
        if webhook.get("url"):
            raise SystemExit(
                "У бота настроен webhook. Проверьте отдельный токен счётчика."
            )
        bot = Bot(settings, with_store, api, identity["username"])
        api.call("deleteMyCommands")
        commands = [{"command": "sober", "description": "Открыть панель"}]
        scopes = {b.chat_id for b in settings.bindings} | {
            s.user_id for s in settings.subjects
        }
        for chat_id in with_store.command_scopes() - scopes:
            try:
                api.call(
                    "deleteMyCommands", {"scope": {"type": "chat", "chat_id": chat_id}}
                )
            except ApiError as exc:
                if exc.code not in (400, 403):
                    raise
        for chat_id in scopes:
            try:
                api.call(
                    "setMyCommands",
                    {
                        "commands": commands,
                        "scope": {"type": "chat", "chat_id": chat_id},
                    },
                )
            except ApiError as exc:
                # Install a private menu on /start if the owner has not opened this bot yet.
                if chat_id < 0 or exc.code != 400:
                    raise
        with_store.save_scopes(scopes)
        backoff = 1
        while running:
            try:
                with_store.tick(
                    datetime.now(UTC), {s.user_id for s in settings.subjects}
                )
                with operation("outbox"):
                    bot.flush()
                with operation("polling"):
                    updates = api.call(
                        "getUpdates",
                        {
                            "offset": with_store.offset(),
                            "timeout": 25,
                            "allowed_updates": ["message", "callback_query"],
                        },
                        timeout=35,
                    )
                backoff = 1
                for update in updates:
                    if not running:
                        break
                    try:
                        bot.handle(update)
                    except ApiError as exc:
                        if exc.code not in (400, 403):
                            raise
                        log.warning(
                            "Ответ Telegram отклонён: method=%s code=%s error=%s",
                            exc.method,
                            exc.code,
                            exc.description or "описание отсутствует",
                        )
                    except (ValueError, KeyError, TypeError):
                        log.warning("Некорректный update (содержимое скрыто)")
                    with_store.advance(update["update_id"])
            except ApiError as exc:
                if exc.code == 409:
                    log.error(
                        "Conflict: этот токен уже запущен другим процессом. Остановите вторую копию; повторов polling не будет."
                    )
                    while running:
                        time.sleep(1)
                elif exc.code in (401, 404):
                    raise SystemExit(
                        "Токен отклонён Telegram (значение скрыто)."
                    ) from None
                else:
                    log.warning("Telegram API error: %s", exc.code)
                    time.sleep(min(30, exc.retry_after or backoff))
                    backoff = min(30, backoff * 2)
            except NetworkError:
                log.warning("Telegram недоступен; состояние сохранено")
                time.sleep(backoff)
                backoff = min(30, backoff * 2)
    except (ApiError, NetworkError) as exc:
        log.error("Запуск Telegram API остановлен: %s", type(exc).__name__)
        raise SystemExit(
            "Не удалось запустить Telegram API (значения скрыты)."
        ) from None
    except Exception:
        log.exception("Неожиданная ошибка счётчика")
        raise SystemExit(
            "Счётчик остановлен из-за ошибки; подробности в логах."
        ) from None
    finally:
        with_store.close()
        log.info("Счётчик остановлен; состояние сохранено")


if __name__ == "__main__":
    main()
