from datetime import UTC, date, datetime, time, timedelta

from app.config import local_timezone


def local_now():
    return datetime.now(local_timezone())


def local_today():
    return local_now().date()


def local_date(value: datetime):
    return value.astimezone(local_timezone()).date()


def utc_bounds_for_local_days(start: date, end: date) -> tuple[str, str]:
    zone = local_timezone()
    start_at = datetime.combine(start, time.min, tzinfo=zone).astimezone(UTC)
    end_at = datetime.combine(end + timedelta(days=1), time.min, tzinfo=zone).astimezone(UTC)
    return start_at.isoformat(), end_at.isoformat()
