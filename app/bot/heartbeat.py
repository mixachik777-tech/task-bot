"""
Bot liveness heartbeat в Redis.

Два независимых сигнала:
- `bot:hb:msg`  — `HeartbeatMiddleware` пишет при каждом обработанном update.
                  Точный признак «бот видит входящие апдейты».
- `bot:hb:getme` — `watchdog`-таска в `app.main` пишет раз в минуту после
                   успешного `getMe`. Гарантированный пульс даже когда
                   входящего трафика нет.

Healthcheck (`healthcheck.py`) проверяет `bot:hb:getme` — этот ключ обязан
обновляться независимо от пользовательской активности. `bot:hb:msg` нужен
для отладки: если он есть, а getme нет, watchdog задушен; если есть getme,
а msg нет — апдейты не идут (polling stuck).

`bump()` оставлен как backward-compatible alias на `bump_getme`, чтобы
старый код не упал — но новые вызовы должны использовать явный bump_msg
или bump_getme.
"""

import asyncio
import time

from redis.asyncio import Redis

HEARTBEAT_KEY_MSG = "bot:hb:msg"
HEARTBEAT_KEY_GETME = "bot:hb:getme"
HEARTBEAT_TTL_S = 600
HEARTBEAT_MAX_AGE_S = 180
HEARTBEAT_OP_TIMEOUT_S = 3

# Backward-compat: старый ключ. Записываем тот же timestamp в оба места,
# чтобы внешние утилиты, читающие "bot:heartbeat", не сломались.
HEARTBEAT_KEY_LEGACY = "bot:heartbeat"


async def _set(redis: Redis, key: str) -> None:
    await asyncio.wait_for(
        redis.set(key, str(int(time.time())), ex=HEARTBEAT_TTL_S),
        timeout=HEARTBEAT_OP_TIMEOUT_S,
    )


async def bump_msg(redis: Redis) -> None:
    """Вызов из middleware на входящем update."""
    await _set(redis, HEARTBEAT_KEY_MSG)
    await _set(redis, HEARTBEAT_KEY_LEGACY)


async def bump_getme(redis: Redis) -> None:
    """Вызов из watchdog после успешного getMe."""
    await _set(redis, HEARTBEAT_KEY_GETME)
    await _set(redis, HEARTBEAT_KEY_LEGACY)


async def bump(redis: Redis) -> None:
    """Legacy-alias. Эквивалентен bump_getme. Оставлен на случай если
    что-то ещё в коде вызывает старое имя; новые вызовы — bump_msg/getme."""
    await bump_getme(redis)


async def age_seconds(redis: Redis, key: str = HEARTBEAT_KEY_GETME) -> int | None:
    """Возраст указанного heartbeat-ключа. По умолчанию — getme, его
    проверяет healthcheck."""
    try:
        val = await asyncio.wait_for(redis.get(key), timeout=HEARTBEAT_OP_TIMEOUT_S)
    except (asyncio.TimeoutError, Exception):  # noqa: BLE001
        return None
    if val is None:
        return None
    try:
        return int(time.time()) - int(val)
    except (TypeError, ValueError):
        return None
