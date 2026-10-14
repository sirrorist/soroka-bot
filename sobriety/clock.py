"""Accounting days are Moscow intervals [07:00, next 07:00)."""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

MOSCOW = ZoneInfo("Europe/Moscow")
DAY = timedelta(days=1)
MILESTONES = {
    1: "ДОЛБОЕЕЕЕБ",
    3: "оно живое",
    5: "оп оп живем живем",
    7: "ахуеть, оно смогло...",
    10: "не верю))",
    14: "ну все-все, харош",
    21: "он точно жив?...",
    30: "НУ ЛЕГЕНДА!",
}


def boundary(day: date) -> datetime:
    return datetime.combine(day, time(7), MOSCOW).astimezone(UTC)


def accounting_date(now: datetime) -> date:
    require_aware(now)
    return (now.astimezone(MOSCOW) - timedelta(hours=7)).date()


def require_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("A timezone-aware datetime is required")


def anchor(start: datetime) -> datetime:
    """An incomplete accounting day after reset does not earn a full day."""
    floor = boundary(accounting_date(start))
    return floor if start == floor else boundary(accounting_date(start) + DAY)


def elapsed(start: datetime, now: datetime, holidays: set[date]) -> tuple[int, int]:
    """Return full 07→07 accounting days and elapsed counted hours separately."""
    require_aware(start)
    require_aware(now)
    if now <= start:
        return 0, 0
    first = anchor(start)
    last = boundary(accounting_date(now))
    days = max(0, (accounting_date(now) - first.astimezone(MOSCOW).date()).days)
    seconds = (now - start).total_seconds()
    for day in holidays:
        left, right = boundary(day), boundary(day + DAY)
        if first <= left and right <= last:
            days -= 1
        seconds -= max(0, (min(now, right) - max(start, left)).total_seconds())
    return max(0, days), max(0, int(seconds // 3600))


def milestone_time(start: datetime, count: int, holidays: set[date]) -> datetime:
    cursor = anchor(start).astimezone(MOSCOW).date()
    while count:
        if cursor not in holidays:
            count -= 1
        cursor += DAY
    return boundary(cursor)


def parse_start(text: str, now: datetime) -> datetime:
    for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            day = datetime.strptime(text.strip(), fmt).date()
            break
        except ValueError:
            continue
    else:
        raise ValueError("Введите дату ДД.ММ.ГГГГ или ГГГГ-ММ-ДД.")
    value = boundary(day)
    # Avoid unbounded calculations and dates that precede this tracker use case.
    if day.year < 2000 or value > now:
        raise ValueError("Нужна дата с 2000 года, не позже текущего времени.")
    return value
