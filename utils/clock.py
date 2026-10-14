"""Текущее время для базы и лимитов."""

from datetime import UTC, datetime


def utcnow() -> datetime:
    """Время UTC без tzinfo - так его хранят столбцы DateTime боевой базы.

    Замена datetime.utcnow(), устаревшему с Python 3.12. Время с tzinfo в столбец
    timestamp without time zone asyncpg не запишет.
    """
    return datetime.now(UTC).replace(tzinfo=None)
