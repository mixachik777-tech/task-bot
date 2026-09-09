from datetime import datetime, timedelta, timezone

from app.bot.utils.time import (
    format_delta,
    format_dt_local,
    now_utc,
    parse_user_datetime,
)


def test_parse_returns_none_for_garbage():
    assert parse_user_datetime("") is None
    assert parse_user_datetime("   ") is None
    assert parse_user_datetime("blah blah") is None


def test_parse_relative_hours_returns_future_utc():
    parsed = parse_user_datetime("через 2 часа")
    assert parsed is not None
    assert parsed.tzinfo is not None
    delta = parsed - now_utc()
    # Should be ~2h ahead, allow ±5 min skew
    assert timedelta(hours=1, minutes=55) < delta < timedelta(hours=2, minutes=5)


def test_parse_tomorrow_evening_is_in_future():
    parsed = parse_user_datetime("завтра 18:00")
    assert parsed is not None
    assert parsed > now_utc()


def test_format_dt_local_round_trips_pattern():
    dt_utc = datetime(2026, 5, 12, 9, 0, tzinfo=timezone.utc)
    out = format_dt_local(dt_utc)
    # Europe/Moscow = UTC+3 → 12:00
    assert out == "12.05.2026 12:00"


def test_format_dt_local_accepts_naive_as_utc():
    dt_naive = datetime(2026, 5, 12, 9, 0)
    assert format_dt_local(dt_naive) == "12.05.2026 12:00"


def test_format_delta_future_days_hours():
    # 2 days 3 hours
    assert format_delta(2 * 86400 + 3 * 3600) == "через 2д 3ч"


def test_format_delta_past_minutes_only():
    # -45 min
    assert format_delta(-45 * 60) == "просрочено на 45мин"


def test_format_delta_zero():
    assert "меньше минуты" in format_delta(10)
