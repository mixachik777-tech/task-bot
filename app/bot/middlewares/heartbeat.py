"""
HeartbeatMiddleware — обновляет Redis-heartbeat на каждом update.

Подключается ПЕРВЫМ в цепочке middleware'ов, чтобы факт «бот живой
и получает события» фиксировался даже если другие middleware'ы упадут.
"""

from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject
from loguru import logger
from redis.asyncio import Redis

from app.bot import heartbeat


class HeartbeatMiddleware(BaseMiddleware):
    def __init__(self, redis: Redis) -> None:
        self.redis = redis

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        try:
            await heartbeat.bump_msg(self.redis)
        except Exception as exc:  # noqa: BLE001
            logger.warning("heartbeat bump_msg failed: {}", exc)
        return await handler(event, data)
