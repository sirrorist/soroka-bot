"""Original wording is literal; extra UI labels are kept separate."""

from datetime import datetime
from html import escape

from .clock import MILESTONES, MOSCOW

RESET_BUTTON = "Ты долбоеб, давай сначала"
HOLIDAY_BUTTON = "Сегодня праздник"
START_BUTTON = "Начало даты с которой вести счёт"
MESSAGE_BUTTON = "Моё сообщение"


def status(snapshot):
    return (
        f"Статус @{escape(snapshot.username)}:\n"
        f"- не пил: {snapshot.days} день\n"
        f"- рекорд: {snapshot.record} дней\n"
        f"- звание: {snapshot.title}"
    )


def reset_status(before, after):
    message = f"<b>{escape(before.message)}</b>\n" if before.message else ""
    return message + (
        f"Статус @{escape(after.username)}:\n"
        f"- не пил: {after.days} день\n"
        f"- рекорд: {after.record} дней\n"
        f"- звание: {before.title} (изменено на) -> {after.title}\n\n"
        f"Было: {before.days} дней → стало: {after.days} дней"
    )


def collection(snapshot):
    rows = [status(snapshot), "", "Коллекция достижений:"]
    awards = {item["threshold"]: item["earned_at"] for item in snapshot.awards}
    for day, title in MILESTONES.items():
        if day in awards:
            earned = datetime.fromisoformat(awards[day]).astimezone(MOSCOW)
            rows.append(f"✓ {day}: {title} — {earned:%d.%m.%Y}")
        else:
            rows.append(f"○ {day}: {title}")
    return "\n".join(rows)


def gift_state(gift):
    if gift["cancelled_at"]:
        return "обещание отменено"
    if gift["received_at"]:
        return "получен"
    if gift["eligible_at"]:
        return "условие выполнено"
    return "обещан"


def gifts_view(snapshot, gifts):
    rows = [status(snapshot), "", "Подарки:"]
    for gift in gifts:
        rows.extend(
            [
                f"#{gift['id']} — за {gift['threshold']} дней: {escape(gift['description'])}",
                f"От: {escape(gift['donor_name'])}; {gift_state(gift)}",
            ]
        )
    if not gifts:
        rows.append("Пока нет подарков.")
    return "\n".join(rows)


def button(label, action, uid, extra=None):
    data = f"sb:{action}:{uid}" + (f":{extra}" if extra is not None else "")
    return {"text": label, "callback_data": data}


def panel_buttons(uid, subjects=(), owner=False, bot_username=None):
    rows = [
        [
            button("Статус", "status", uid),
            button("Коллекция", "collection", uid),
            button("Подарки", "gifts", uid),
        ]
    ]
    if owner:
        rows.extend(
            [
                [
                    button(START_BUTTON, "date", uid),
                    button(MESSAGE_BUTTON, "message", uid),
                ],
                [button(RESET_BUTTON, "reset", uid)],
                [
                    button(HOLIDAY_BUTTON, "holiday", uid),
                    button("Отменить праздник", "holidays", uid),
                ],
            ]
        )
    rows.append([button("Обещать подарок", "promise", uid)])
    for subject in subjects:
        rows.append([button(f"@{subject.username}", "status", subject.user_id)])
    if bot_username:
        rows.append(
            [
                {
                    "text": "Моя панель в личке",
                    "url": f"https://t.me/{bot_username}?start=panel",
                }
            ]
        )
    return {"inline_keyboard": rows}
