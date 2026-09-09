"""
Глобальный accessor бота. Парный к app/scheduler/runtime.py.

Используется job-функциями APScheduler, которым неудобно получать
Bot через args (см. DECISIONS.md → Этап 6).

Инициализация в app/main.py: set_bot(bot) сразу после Bot(...).
"""

from aiogram import Bot
from redis.asyncio import Redis

_bot: Bot | None = None
_redis: Redis | None = None


def set_bot(bot: Bot) -> None:
    global _bot
    _bot = bot


def get_bot() -> Bot:
    if _bot is None:
        raise RuntimeError("Bot not initialized — call set_bot() first")
    return _bot


def set_redis(redis: Redis) -> None:
    global _redis
    _redis = redis


def get_redis() -> Redis:
    if _redis is None:
        raise RuntimeError("Redis not initialized — call set_redis() first")
    return _redis
