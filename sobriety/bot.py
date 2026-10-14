"""One group command and button panels; changes to a series are private."""

import logging
from datetime import date, datetime
from html import escape

from .api import ApiError, NetworkError
from .clock import UTC, accounting_date, parse_start
from .diagnostics import CALLBACKS, event_action, operation
from .views import (
    HOLIDAY_BUTTON,
    MESSAGE_BUTTON,
    START_BUTTON,
    button,
    collection,
    gifts_view,
    panel_buttons,
    reset_status,
    status,
)

log = logging.getLogger("sobriety")


class Bot:
    def __init__(self, settings, store, api, username, now=None):
        self.settings, self.store, self.api = settings, store, api
        self.username = username
        self.now = now or (lambda: datetime.now(UTC))
        store.register(settings.subjects)

    @staticmethod
    def thread(message):
        return (
            message.get("message_thread_id")
            if message.get("is_topic_message")
            else None
        )

    def permitted(self, actor, message):
        chat = message.get("chat") or {}
        if chat.get("type") == "private":
            if actor != chat.get("id"):
                return set()
            allowed = (
                {actor}
                if actor in {s.user_id for s in self.settings.subjects}
                else set()
            )
            access = self.store.gift_access(actor, self.now())
            if access and access["uid"] in self.settings.subjects_for(
                access["chat"], access["thread"]
            ):
                allowed.add(access["uid"])
            return allowed
        if chat.get("type") in {"group", "supergroup"}:
            return self.settings.subjects_for(chat.get("id"), self.thread(message))
        return set()

    def markup(self, uid, message, actor=None):
        allowed = self.permitted(actor, message)
        private = message["chat"]["type"] == "private"
        subjects = [
            s
            for s in self.settings.subjects
            if s.user_id in allowed and s.user_id != uid
        ]
        return panel_buttons(
            uid,
            subjects,
            owner=private and actor == uid,
            bot_username=None if private else self.username,
        )

    def send(self, message, text, markup=None):
        fields = {
            "chat_id": message["chat"]["id"],
            "message_thread_id": self.thread(message),
            "text": text,
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
            "reply_markup": markup,
        }
        return self.api.call("sendMessage", fields)

    def show(self, actor, message, uid, view="status", edit=False, page=0):
        snapshot = self.store.snapshot(uid, self.now())
        markup = self.markup(uid, message, actor)
        if view == "collection":
            text = collection(snapshot)
        elif view == "gifts":
            gifts = self.store.gifts(uid, page)
            text = gifts_view(snapshot, gifts)
            rows = markup["inline_keyboard"]
            for gift in gifts:
                if (
                    actor == uid
                    and message["chat"]["type"] == "private"
                    and gift["eligible_at"]
                    and not gift["received_at"]
                    and not gift["cancelled_at"]
                ):
                    rows.append(
                        [
                            button(
                                f"Подарок #{gift['id']} получен",
                                "received",
                                uid,
                                gift["id"],
                            )
                        ]
                    )
                elif (
                    actor == gift["donor_id"]
                    and message["chat"]["type"] == "private"
                    and not gift["eligible_at"]
                    and not gift["cancelled_at"]
                ):
                    rows.append(
                        [
                            button(
                                f"Отменить обещание #{gift['id']}",
                                "cancelgift",
                                uid,
                                gift["id"],
                            )
                        ]
                    )
            navigation = []
            if page:
                navigation.append(button("←", "gifts", uid, page - 1))
            if len(gifts) == 5:
                navigation.append(button("→", "gifts", uid, page + 1))
            if navigation:
                rows.append(navigation)
        else:
            text = status(snapshot) + f"\n\nЧасов в зачёт: {snapshot.hours}"
        if snapshot.start is None:
            text += "\n\nДата начала ещё не задана."
        if edit:
            try:
                self.api.call(
                    "editMessageText",
                    {
                        "chat_id": message["chat"]["id"],
                        "message_id": message["message_id"],
                        "text": text,
                        "parse_mode": "HTML",
                        "reply_markup": markup,
                        "link_preview_options": {"is_disabled": True},
                    },
                )
            except ApiError as exc:
                if not exc.message_not_modified:
                    raise
                log.info("Панель уже актуальна; повторное нажатие обработано")
        else:
            self.send(message, text, markup)

    def prompt(
        self,
        actor,
        message,
        kind,
        uid,
        text,
        *,
        processed_key=None,
        sender=None,
        now=None,
        **payload,
    ):
        if message["chat"]["type"] != "private":
            raise ValueError("Ввод доступен только в личке.")
        sender = sender or message.get("from") or {}
        name = (
            "@" + sender["username"]
            if sender.get("username")
            else sender.get("first_name", "Пользователь")[:80]
        )
        payload = {
            "uid": uid,
            "origin": processed_key,
            "prompt_text": text,
            "prompt_name": name,
        } | payload
        self.store.put_dialog(
            actor,
            message["chat"]["id"],
            self.thread(message),
            kind,
            payload,
            0,
            now or self.now(),
            key=processed_key,
        )
        self.deliver_prompt(actor, message, payload)

    def deliver_prompt(self, actor, message, payload):
        text = (
            f'<a href="tg://user?id={actor}">{escape(payload["prompt_name"])}</a>, Ваш ввод\n'
            + payload["prompt_text"]
            + "\n\nОтветьте в течение 5 минут."
        )
        result = self.send(message, text, {"force_reply": True, "selective": True})
        self.store.set_prompt(
            actor,
            message["chat"]["id"],
            self.thread(message),
            payload["origin"],
            result["message_id"],
        )

    def resume_prompt(self, actor, message, key):
        if not self.permitted(actor, message):
            return
        dialog = self.store.dialog(
            actor, message["chat"]["id"], self.thread(message), self.now()
        )
        if dialog and dialog["payload"].get("origin") == key and not dialog["prompt"]:
            self.deliver_prompt(actor, message, dialog["payload"])

    def answer_callback(self, fields):
        try:
            self.api.call("answerCallbackQuery", fields)
            return True
        except ApiError as exc:
            if not exc.callback_expired:
                raise
            log.info(
                "Callback acknowledgement устарел; локальная проверка действия продолжается"
            )
            return False

    def start_gift(self, actor, message, text, key):
        parts = text.split(maxsplit=1)
        if (
            message["chat"]["type"] != "private"
            or actor != message["chat"]["id"]
            or len(parts) != 2
        ):
            return False
        command, _, bot_name = parts[0].partition("@")
        if (
            command != "/start"
            or (bot_name and bot_name.casefold() != self.username.casefold())
            or not parts[1].startswith("gift_")
        ):
            return False
        token = parts[1][5:]
        invite = self.store.invite(token, actor, self.now())
        if not invite or invite["uid"] not in self.settings.subjects_for(
            invite["chat"], invite["thread"]
        ):
            self.send(
                message,
                "Ссылка устарела или не ваша. Откройте подарок кнопкой в группе.",
            )
            return True
        self.store.clear_dialog(actor, actor, self.thread(message))
        self.store.cancel_confirmation(actor, actor)
        self.store.activate_gift(token, actor, self.now())
        target = next(
            s.username for s in self.settings.subjects if s.user_id == invite["uid"]
        )
        self.prompt(
            actor,
            message,
            "gift_days",
            invite["uid"],
            f"Подарок для @{escape(target)}.\nЗа сколько дней Вы обещаете подарок? Введите N:",
            processed_key=key,
            gift_token=token,
        )
        return True

    def handle(self, update):
        with operation(event_action(update)):
            return self._handle(update)

    def _handle(self, update):
        callback = update.get("callback_query")
        if callback:
            return self.callback(callback, update.get("update_id"))
        message = update.get("message")
        if not isinstance(message, dict):
            return
        sender = message.get("from") or {}
        actor = sender.get("id")
        if message.get("sender_chat"):
            log.info(
                "Сообщение пропущено: reason=sender_chat; отправьте команду от личного аккаунта, а не от имени чата"
            )
            return
        if sender.get("is_bot"):
            log.info("Сообщение пропущено: reason=sender_is_bot")
            return
        if type(actor) is not int:
            log.info("Сообщение пропущено: reason=sender_without_id")
            return
        text = message.get("text")
        key = f"message:{update['update_id']}"
        if self.store.processed(key):
            self.resume_prompt(actor, message, key)
            log.info("Повторный ввод уже обработан; состояние диалога не изменено")
            return
        if isinstance(text, str) and self.start_gift(actor, message, text, key):
            return
        allowed = self.permitted(actor, message)
        if not allowed:
            chat = message.get("chat") or {}
            if chat.get("type") == "private":
                log.info(
                    "Сообщение пропущено: reason=private_not_allowed; пользователь не разрешён в SOBRIETY_USERS"
                )
            elif chat.get("type") in {"group", "supergroup"}:
                if any(b.chat_id == chat.get("id") for b in self.settings.bindings):
                    log.info(
                        "Сообщение пропущено: reason=topic_not_allowed; чат настроен, но этот топик не разрешён; проверьте thread_id"
                    )
                else:
                    log.info(
                        "Сообщение пропущено: reason=chat_not_allowed; в этом чате нет привязок SOBRIETY_BINDINGS; после изменения env пересоздайте контейнер"
                    )
            else:
                log.info("Сообщение пропущено: reason=unsupported_chat_type")
            return
        text = message.get("text")
        if not isinstance(text, str):
            log.info("Сообщение пропущено: reason=message_without_text")
            return
        if text.startswith("/"):
            command, *args = text.split(maxsplit=1)
            base, _, bot_name = command.partition("@")
            if bot_name and bot_name.casefold() != self.username.casefold():
                log.info(
                    "Команда пропущена: reason=command_for_other_bot; укажите username счётчика после @"
                )
                return
            private = message["chat"]["type"] == "private"
            if base not in ({"/sober", "/start"} if private else {"/sober"}):
                log.info(
                    "Команда пропущена: reason=command_not_supported; в группе доступна /sober"
                )
                return
            if private:
                self.api.call(
                    "setMyCommands",
                    {
                        "commands": [
                            {"command": "sober", "description": "Открыть панель"}
                        ],
                        "scope": {"type": "chat", "chat_id": actor},
                    },
                )
            self.store.clear_dialog(actor, message["chat"]["id"], self.thread(message))
            if private:
                self.store.cancel_confirmation(actor, message["chat"]["id"])
                if actor not in {s.user_id for s in self.settings.subjects}:
                    access = self.store.gift_access(actor, self.now())
                    self.show(actor, message, access["uid"], "gifts")
                    return
                self.store.clear_gift_access(actor)
            uid = actor if private else None
            if not private and args:
                name = args[0].lstrip("@").strip().casefold()
                uid = next(
                    (
                        s.user_id
                        for s in self.settings.subjects
                        if s.username.casefold() == name and s.user_id in allowed
                    ),
                    None,
                )
                if uid is None:
                    self.send(message, "Пользователь не найден в этом чате.")
                    return
            elif not private:
                reply_actor = (
                    (message.get("reply_to_message") or {}).get("from") or {}
                ).get("id")
                if reply_actor in allowed:
                    uid = reply_actor
                elif len(allowed) == 1:
                    uid = next(iter(allowed))
            if uid is None:
                rows = [
                    [button(f"@{s.username}", "status", s.user_id)]
                    for s in self.settings.subjects
                    if s.user_id in allowed
                ]
                self.send(message, "Выберите пользователя:", {"inline_keyboard": rows})
            else:
                self.show(actor, message, uid)
            return
        if message["chat"]["type"] != "private":
            log.info("Сообщение пропущено: reason=group_view_only")
            return
        dialog = self.store.dialog(
            actor, message["chat"]["id"], self.thread(message), self.now()
        )
        if not dialog:
            log.info("Сообщение пропущено: reason=no_input_dialog")
            return
        if (
            message.get("reply_to_message")
            and dialog["prompt"]
            and message["reply_to_message"].get("message_id") != dialog["prompt"]
        ):
            log.info("Сообщение пропущено: reason=reply_to_wrong_prompt")
            return
        if dialog["payload"]["uid"] not in allowed:
            log.info("Сообщение пропущено: reason=subject_not_allowed")
            return
        key = f"message:{update['update_id']}"
        try:
            with operation("input:" + dialog["kind"]):
                self.input(actor, sender, message, text, dialog, key)
        except ValueError as exc:
            self.send(
                message,
                escape(str(exc)),
                self.markup(dialog["payload"]["uid"], message, actor),
            )

    def input(self, actor, sender, message, text, dialog, key):
        uid, kind, now = dialog["payload"]["uid"], dialog["kind"], self.now()
        private_owner = message["chat"]["type"] == "private" and uid == actor
        if kind in {"date", "message", "holiday"} and not private_owner:
            return
        completed_dialog = (actor, message["chat"]["id"], self.thread(message))
        if kind == "date":
            self.store.set_start(
                uid, parse_start(text, now), now, key, dialog=completed_dialog
            )
        elif kind == "message":
            if not text or len(text) > 1000:
                raise ValueError("Сообщение должно быть от 1 до 1000 символов.")
            self.store.set_message(uid, text, key, dialog=completed_dialog)
        elif kind == "holiday":
            if not text.strip() or len(text) > 100:
                raise ValueError("Название праздника должно быть от 1 до 100 символов.")
            self.store.add_holiday(
                uid,
                text,
                now,
                key,
                day=date.fromisoformat(dialog["payload"]["day"])
                if dialog["payload"].get("day")
                else None,
                dialog=completed_dialog,
            )
        elif kind == "gift_days":
            if (
                not text.isascii()
                or not text.isdecimal()
                or not 1 <= int(text) <= 36500
            ):
                raise ValueError("Введите число дней от 1 до 36500.")
            self.prompt(
                actor,
                message,
                "gift_description",
                uid,
                "Какой подарок Вы обещаете?",
                processed_key=key,
                sender=sender,
                gift_token=dialog["payload"].get("gift_token"),
                threshold=int(text),
            )
            return
        elif kind == "gift_description":
            if not text.strip() or len(text) > 300:
                raise ValueError("Описание подарка должно быть от 1 до 300 символов.")
            name = (sender.get("username") or sender.get("first_name") or "Участник")[
                :80
            ]
            self.store.add_gift(
                uid,
                actor,
                name,
                dialog["payload"]["threshold"],
                text,
                now,
                key,
                dialog=completed_dialog,
            )
        else:
            return
        self.show(
            actor, message, uid, "gifts" if kind == "gift_description" else "status"
        )

    def callback(self, callback, update_id):
        now = self.now()
        message = callback.get("message")
        actor = (callback.get("from") or {}).get("id")
        data = callback.get("data")
        if (
            not isinstance(message, dict)
            or type(actor) is not int
            or not isinstance(data, str)
        ):
            return
        allowed = self.permitted(actor, message)
        parts = data.split(":")
        if (
            len(parts) not in (3, 4)
            or parts[0] != "sb"
            or not parts[2].isascii()
            or not parts[2].isdecimal()
        ):
            return
        action, uid, extra = (
            parts[1],
            int(parts[2]),
            parts[3] if len(parts) == 4 else None,
        )
        if action not in CALLBACKS:
            return
        if uid not in allowed:
            self.answer_callback(
                {
                    "callback_query_id": callback["id"],
                    "text": "Нет доступа.",
                    "show_alert": True,
                },
            )
            return
        if action == "promise" and message["chat"]["type"] != "private":
            token = self.store.gift_invite(
                uid,
                actor,
                message["chat"]["id"],
                self.thread(message),
                now,
                f"callback:{update_id}",
            )
            url = f"https://t.me/{self.username}?start=gift_{token}"
            profile = callback.get("from") or {}
            name = (
                "@" + profile["username"]
                if profile.get("username")
                else profile.get("first_name", "Пользователь")[:80]
            )
            if not self.answer_callback(
                {
                    "callback_query_id": callback["id"],
                    "url": url,
                    "text": f"{name}, продолжите в личке бота.",
                }
            ):
                self.send(
                    message,
                    f"{escape(name)}, продолжите в личке бота.",
                    {
                        "inline_keyboard": [
                            [{"text": "Обещать подарок в личке", "url": url}]
                        ]
                    },
                )
            return
        private_owner = message["chat"]["type"] == "private" and uid == actor
        if (
            action
            in {
                "date",
                "message",
                "holiday",
                "holidays",
                "unholiday",
                "reset",
                "confirm",
                "received",
            }
            and not private_owner
        ):
            self.answer_callback(
                {
                    "callback_query_id": callback["id"],
                    "text": "Управление доступно владельцу в личке.",
                    "show_alert": True,
                },
            )
            return
        if message["chat"]["type"] != "private" and action == "cancelgift":
            self.answer_callback(
                {
                    "callback_query_id": callback["id"],
                    "text": "Управление подарками доступно в личке.",
                    "show_alert": True,
                }
            )
            return
        self.answer_callback({"callback_query_id": callback["id"]})
        key = f"callback:{update_id}"
        if self.store.processed(key):
            self.resume_prompt(actor, message, key)
            return
        if message["chat"]["type"] == "private" and action != "confirm":
            self.store.cancel_confirmation(actor, message["chat"]["id"])
        if message["chat"]["type"] == "private" and action != "confirm":
            self.store.clear_dialog(actor, message["chat"]["id"], self.thread(message))
        try:
            if action in {"status", "collection", "gifts"}:
                page = (
                    int(extra)
                    if action == "gifts" and extra and extra.isdecimal()
                    else 0
                )
                self.show(actor, message, uid, action, edit=True, page=min(page, 10000))
            elif action in {"date", "message", "holiday"}:
                prompt = {
                    "date": START_BUTTON + " (ДД.ММ.ГГГГ):",
                    "message": MESSAGE_BUTTON + ":",
                    "holiday": HOLIDAY_BUTTON + ". Введите название:",
                }[action]
                if action == "date" and not self.store.can_set_start(uid):
                    raise ValueError(
                        "Дата уже задана. Новую серию начните кнопкой сброса."
                    )
                if action == "holiday" and self.store.snapshot(uid, now).start is None:
                    raise ValueError("Сначала задайте дату начала.")
                self.prompt(
                    actor,
                    message,
                    action,
                    uid,
                    prompt,
                    processed_key=key,
                    sender=callback.get("from"),
                    now=now,
                    **(
                        {"day": accounting_date(now).isoformat()}
                        if action == "holiday"
                        else {}
                    ),
                )
            elif action == "promise":
                self.prompt(
                    actor,
                    message,
                    "gift_days",
                    uid,
                    "За сколько дней Вы обещаете подарок? Введите N:",
                    processed_key=key,
                    sender=callback.get("from"),
                    gift_token=(self.store.gift_access(actor, now) or {}).get("token"),
                )
            elif action == "holidays":
                holidays = self.store.holidays(uid)
                rows = [
                    [button("Отменить " + item["day"], "unholiday", uid, item["day"])]
                    for item in holidays
                ]
                rows += self.markup(uid, message, actor)["inline_keyboard"]
                self.send(
                    message,
                    "Выберите ошибочно отмеченный праздник:\n"
                    + "\n".join(
                        item["day"] + " — " + escape(item["name"]) for item in holidays
                    ),
                    {"inline_keyboard": rows},
                )
            elif action == "unholiday" and extra:
                self.store.cancel_holiday(uid, extra, now, key)
                self.show(actor, message, uid, edit=True)
            elif action == "reset":
                token = self.store.confirmation(uid, message["chat"]["id"], now)
                snapshot = self.store.snapshot(uid, now)
                rows = [
                    [
                        button("Подтвердить сброс", "confirm", uid, token),
                        button("Отмена", "status", uid),
                    ]
                ]
                self.send(
                    message,
                    status(snapshot) + "\n\nПодтвердить сброс счётчика до 0 дней?",
                    {"inline_keyboard": rows},
                )
            elif action == "confirm" and extra:
                self.store.reset(
                    uid, message["chat"]["id"], extra, now, key, self.notifications
                )
                self.show(actor, message, uid, edit=True)
            elif action == "received" and extra and extra.isdecimal():
                self.store.receive_gift(uid, int(extra), now, key)
                self.show(actor, message, uid, "gifts", edit=True)
            elif action == "cancelgift" and extra and extra.isdecimal():
                self.store.cancel_gift(uid, actor, int(extra), now, key)
                self.show(actor, message, uid, "gifts", edit=True)
        except ValueError as exc:
            self.send(message, escape(str(exc)), self.markup(uid, message, actor))

    def notifications(self, before, after):
        for binding in self.settings.bindings:
            if binding.user_id != after.user_id:
                continue
            subjects = [
                s
                for s in self.settings.subjects
                if s.user_id
                in self.settings.subjects_for(binding.chat_id, binding.thread_id)
                and s.user_id != after.user_id
            ]
            yield {
                "chat_id": binding.chat_id,
                "message_thread_id": binding.thread_id,
                "text": reset_status(before, after),
                "parse_mode": "HTML",
                "reply_markup": panel_buttons(
                    after.user_id, subjects, bot_username=self.username
                ),
                "link_preview_options": {"is_disabled": True},
            }

    def flush(self):
        for item in self.store.pending(self.now()):
            # Removed configuration cannot keep publishing to an old binding.
            subject = item["payload"]["user_id"]
            payload = item["payload"]["message"]
            if not any(
                b.user_id == subject
                and b.chat_id == payload["chat_id"]
                and b.thread_id == payload.get("message_thread_id")
                for b in self.settings.bindings
            ):
                self.store.sent(item["id"])
                continue
            try:
                self.api.call("sendMessage", payload)
            except ApiError as exc:
                if exc.code in (401, 404):
                    raise
                if exc.code not in (400, 403):
                    delay = self.store.retry(item["id"], self.now(), exc.retry_after)
                    log.warning(
                        "Публикация отложена: Telegram %s; повтор через %s секунд",
                        exc.code,
                        delay,
                    )
                    break
                log.warning("Публикация недоступна: Telegram %s", exc.code)
            except NetworkError:
                delay = self.store.retry(item["id"], self.now())
                log.warning(
                    "Публикация отложена: ошибка соединения; повтор через %s секунд",
                    delay,
                )
                break
            self.store.sent(item["id"])
