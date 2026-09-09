"""
Парсинг и форматирование дат для UI.

Контракт:
- В БД и в коде всегда UTC (timezone-aware).
- Пользователю время показывается в локальной зоне (settings.TZ,
  по умолчанию Europe/Moscow) через format_dt_local().
- dateparser принимает русские относительные даты («завтра 18:00»,
  «через 2 часа», «15.05 14:00»). На неоднозначных датах
  PREFER_DATES_FROM=future сдвигает в будущее.
"""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import dateparser

from app.config import settings


def _local_tz() -> ZoneInfo:
    return ZoneInfo(settings.TZ)


def parse_user_datetime(text: str) -> datetime | None:
    """
    Парсит русскую дату/время из текста пользователя.
    Возвращает UTC datetime (aware) или None, если не распознано.
    """
    if not text or not text.strip():
        return None
    parsed = dateparser.parse(
        text.strip(),
        languages=["ru"],
        settings={
            "TIMEZONE": settings.TZ,
            "RETURN_AS_TIMEZONE_AWARE": True,
            "PREFER_DATES_FROM": "future",
        },
    )
    if parsed is None:
        return None
    return parsed.astimezone(timezone.utc)


def format_dt_local(dt: datetime) -> str:
    """Форматирует UTC datetime в локальное «DD.MM.YYYY HH:MM»."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_local_tz()).strftime("%d.%m.%Y %H:%M")


def format_delta(delta_seconds: int) -> str:
    """
    Человекочитаемая дельта от now.
    delta_seconds > 0 → «через …»; < 0 → «просрочено на …».
    """
    sign = "через" if delta_seconds >= 0 else "просрочено на"
    total = abs(int(delta_seconds))
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}д")
    if hours:
        parts.append(f"{hours}ч")
    if not days and minutes:
        parts.append(f"{minutes}мин")
    if not parts:
        parts.append("меньше минуты")
    return f"{sign} {' '.join(parts)}"


def now_utc() -> datetime:
    return datetime.now(timezone.utc)
