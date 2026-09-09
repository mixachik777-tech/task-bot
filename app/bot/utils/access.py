"""
Хелперы доступа на основе РЕАЛЬНЫХ Telegram-прав в супергруппе.

В task-bot есть две независимых «роли»:
- `User.role` в БД — formal-роль (employee/lead/admin), хранится для UI
  и `require_role`-middleware. Может быть выставлена сидером или вручную.
- Реальная Telegram-роль участника супергруппы — `creator/administrator/
  member` — отдаётся `bot.get_chat_administrators(chat_id)`.

Действия, которые меняют структуру (распределение людей по отделам,
управление настройками чата) опираются на Telegram-роль, а не на БД-флаг —
чтобы случайный «admin в БД» без реальных прав в чате не мог трогать
других сотрудников.

Кеширование: список TG-админов хранится в Redis по ключу
`tg_admins:{chat_id}` как SET'ом tg_user_id, TTL `_CACHE_TTL_S` (5 мин).
Получение списка через Bot API стоит ~150мс и rate-limited.
"""

from __future__ import annotations

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from loguru import logger
from redis.asyncio import Redis

_CACHE_TTL_S = 5 * 60
_CACHE_KEY = "tg_admins:{chat_id}"
TELEGRAM_ADMIN_STATUSES = {"creator", "administrator"}


async def _load_admin_ids(bot: Bot, chat_id: int) -> set[int] | None:
    """Подтянуть актуальный список с Telegram. None при ошибке API."""
    try:
        admins = await bot.get_chat_administrators(chat_id=chat_id)
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        logger.warning("get_chat_administrators({}) failed: {}", chat_id, exc)
        return None
    except TelegramAPIError as exc:
        logger.warning("get_chat_administrators({}) telegram error: {}", chat_id, exc)
        return None
    return {
        m.user.id
        for m in admins
        if m.status in TELEGRAM_ADMIN_STATUSES and not m.user.is_bot
    }


async def get_telegram_admin_ids(
    bot: Bot, redis: Redis, chat_id: int, *, force_refresh: bool = False
) -> set[int]:
    """
    ID реальных админов супергруппы (creator + administrator), без ботов.
    Кешируется на `_CACHE_TTL_S`. Возврат пустого set'а при ошибке —
    безопасный дефолт (никто не пройдёт проверку).

    force_refresh=True — обновить кеш принудительно (после `/promote`,
    например, если такой команды у нас появится).
    """
    if not chat_id:
        return set()
    key = _CACHE_KEY.format(chat_id=chat_id)
    if not force_refresh:
        cached = await redis.smembers(key)
        if cached:
            try:
                return {int(x) for x in cached}
            except ValueError:
                logger.warning("tg_admins cache corrupted, refreshing")
    ids = await _load_admin_ids(bot, chat_id)
    if ids is None:
        return set()
    pipe = redis.pipeline()
    pipe.delete(key)
    if ids:
        pipe.sadd(key, *[str(x) for x in ids])
        pipe.expire(key, _CACHE_TTL_S)
    await pipe.execute()
    return ids


async def is_telegram_admin(
    bot: Bot, redis: Redis, chat_id: int, tg_user_id: int
) -> bool:
    """Удобный shortcut: проверка одного юзера."""
    ids = await get_telegram_admin_ids(bot, redis, chat_id)
    return tg_user_id in ids
