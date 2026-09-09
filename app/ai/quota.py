"""
Per-user дневной лимит AI-вопросов через Redis.

Защищает общую квоту Gemini API от исчерпания одним активным пользователем.
Лимит настраивается через `settings.AI_USER_DAILY_LIMIT`. 0 = выключено.

Ключ: `ai_quota:{user_id}` (TTL до конца суток по локальной TZ).
Значение — счётчик запросов.

Используется в handler-е BEFORE вызова GeminiAgent: если квота исчерпана,
пользователь получает дружелюбное сообщение, запрос к API не делается.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from redis.asyncio import Redis

from app.config import settings

KEY_PATTERN = "ai_quota:{user_id}"


def _seconds_until_midnight(now: datetime | None = None) -> int:
    tz = ZoneInfo(settings.TZ)
    n = (now or datetime.now(tz)).astimezone(tz)
    tomorrow = (n + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return max(60, int((tomorrow - n).total_seconds()))


async def check_and_increment(redis: Redis, user_id: int) -> tuple[bool, int, int]:
    """
    Атомарно инкрементит счётчик дневных AI-запросов юзера.

    Returns:
        (allowed, current_count, daily_limit). При daily_limit=0 — всегда
        allowed=True, считаем для статистики, не блокируем.
    """
    limit = settings.AI_USER_DAILY_LIMIT
    key = KEY_PATTERN.format(user_id=user_id)
    pipe = redis.pipeline()
    pipe.incr(key)
    pipe.expire(key, _seconds_until_midnight(), nx=True)
    incr_result, _ = await pipe.execute()
    count = int(incr_result)
    if limit <= 0:
        return True, count, limit
    if count > limit:
        return False, count, limit
    return True, count, limit


async def get_remaining(redis: Redis, user_id: int) -> int:
    """Сколько ещё вопросов доступно сегодня (для диагностики/UI)."""
    limit = settings.AI_USER_DAILY_LIMIT
    if limit <= 0:
        return -1
    key = KEY_PATTERN.format(user_id=user_id)
    raw = await redis.get(key)
    used = int(raw) if raw else 0
    return max(0, limit - used)
