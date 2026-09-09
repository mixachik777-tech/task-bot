"""
Docker HEALTHCHECK скрипт.

Проверяет heartbeat watchdog'а (`bot:hb:getme`) — его пишет background-job,
который раз в минуту дёргает getMe и тем самым доказывает, что polling-цикл
жив. Этот сигнал независим от входящего трафика: даже если апдейтов нет,
ключ обязан обновляться. Если он устарел — polling в реальности задушен.

Exit:
- 0 — heartbeat свежий (моложе HEARTBEAT_MAX_AGE_S)
- 1 — heartbeat отсутствует, устарел или Redis недоступен
"""

import os
import sys
import time

import redis

HEARTBEAT_KEY_GETME = "bot:hb:getme"
HEARTBEAT_KEY_LEGACY = "bot:heartbeat"  # fallback на случай миграции
HEARTBEAT_MAX_AGE_S = 180


def main() -> int:
    try:
        client = redis.Redis(
            host=os.getenv("REDIS_HOST", "redis"),
            port=int(os.getenv("REDIS_PORT", "6379")),
            db=int(os.getenv("REDIS_DB", "0")),
            socket_timeout=3,
            socket_connect_timeout=3,
        )
        raw = client.get(HEARTBEAT_KEY_GETME)
        if raw is None:
            # Бот мог не успеть обновить новый ключ после рестарта — пробуем
            # legacy на одну итерацию, чтобы не дать autoheal'у убить
            # только что стартовавший процесс.
            raw = client.get(HEARTBEAT_KEY_LEGACY)
    except Exception as exc:  # noqa: BLE001
        print(f"healthcheck: redis error: {exc}", file=sys.stderr)
        return 1

    if raw is None:
        print("healthcheck: heartbeat missing", file=sys.stderr)
        return 1

    try:
        ts = int(raw)
    except (TypeError, ValueError):
        print(f"healthcheck: invalid heartbeat value: {raw!r}", file=sys.stderr)
        return 1

    age = int(time.time()) - ts
    if age > HEARTBEAT_MAX_AGE_S:
        print(
            f"healthcheck: heartbeat stale ({age}s > {HEARTBEAT_MAX_AGE_S}s)",
            file=sys.stderr,
        )
        return 1

    print(f"healthcheck: ok (age {age}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
