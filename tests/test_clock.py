"""utcnow: UTC без tzinfo, как столбцы DateTime боевой базы."""

from datetime import UTC, datetime, timedelta

from utils.clock import utcnow


def test_utcnow_is_naive_utc():
    now = utcnow()
    assert now.tzinfo is None
    real = datetime.now(UTC).replace(tzinfo=None)
    assert abs(real - now) < timedelta(seconds=5)
