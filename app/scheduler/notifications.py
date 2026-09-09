"""
Helpers для уведомлений leads/admin о просрочках с Redis-дедупликацией.

Redis-клиент инжектируется через set_redis() в main.py — переиспользуется
тот же инстанс, что бот использует для FSM storage (не плодим коннекты).

Ключ: `overdue_notif:<task_id>`  TTL: OVERDUE_DEDUP_TTL_SECONDS (24h).
Содержимое — пустая строка "1" (важен только факт наличия ключа).

mark_overdue_notified возвращает True, если ключ был установлен (т.е. это
ПЕРВЫЙ раз за TTL), False — если уже был. Каноническая semantics:
"first-time SET wins" через `SET key value NX EX <ttl>`.
"""

from typing import Sequence

from loguru import logger
from redis.asyncio import Redis

from app.bot.utils.tg import send_dm_safe
from app.db.models import User

OVERDUE_DEDUP_TTL_SECONDS = 24 * 60 * 60  # 24h
REDIS_KEY_OVERDUE = "overdue_notif:{task_id}"

_redis: Redis | None = None


def set_redis(client: Redis) -> None:
    global _redis
    _redis = client


def get_redis() -> Redis:
    if _redis is None:
        raise RuntimeError("Redis not initialized — call set_redis() first")
    return _redis


async def mark_overdue_notified(task_id: int) -> bool:
    """
    Возвращает True, если уведомить НАДО (ключ только что установлен).
    False — если за TTL уже уведомляли (skip).
    """
    redis = get_redis()
    key = REDIS_KEY_OVERDUE.format(task_id=task_id)
    # SET NX EX — атомарно: установит и вернёт True, либо ничего и False.
    result = await redis.set(name=key, value="1", ex=OVERDUE_DEDUP_TTL_SECONDS, nx=True)
    return bool(result)


async def notify_overdue(
    bot, task_id: int, recipients: Sequence[User], text: str
) -> int:
    """
    Шлёт DM каждому recipient с отключённым превью ссылки
    (в тексте overdue-уведомления может быть ссылка на сообщение задачи —
    превью занимает половину экрана).
    Возвращает число успешных доставок.
    """
    delivered = 0
    for user in recipients:
        ok = await send_dm_safe(
            bot,
            chat_id=user.tg_user_id,
            text=text,
            disable_link_preview=True,
        )
        if ok:
            delivered += 1
        else:
            # PII (имя/tg_id) не логируем — пишем только внутренний user_id.
            logger.warning(
                "overdue notification to user_id={} (task_id={}) failed",
                user.id, task_id,
            )
    return delivered
