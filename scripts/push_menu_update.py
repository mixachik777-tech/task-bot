"""
Разовая миграция reply-клавиатуры главного меню для существующих пользователей.

Зачем нужен: Telegram кеширует ReplyKeyboardMarkup на стороне клиента. После
изменения раскладки кнопок (добавили «👤 Кабинет», «📊 Статистика») старая
клавиатура остаётся у тех, кому бот не присылал сообщение с новым reply_markup.
MenuAwareBot обновляет лениво — только при штатном ответе бота. Этот скрипт
делает то же самое, но активно: разово шлёт каждому approved+active юзеру
одно сообщение с новым reply_markup и помечает в Redis.

Идемпотентность: пропускает пользователей, у которых Redis-ключ
`menu:ver:<tg_id>` уже равен MENU_VERSION. Безопасно перезапускать.

Запуск (при поднятом docker compose):
    docker compose exec bot python -m scripts.push_menu_update
"""

from __future__ import annotations

import asyncio

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import (
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from loguru import logger
from redis.asyncio import Redis
from sqlalchemy import select

from app.bot.keyboards.main_menu import MENU_VERSION, build_main_menu
from app.bot.menu_refresh import MENU_VERSION_KEY_FMT, MENU_VERSION_TTL_S
from app.config import settings
from app.db.base import async_session_factory
from app.db.enums import AccessStatus, UserRole
from app.db.models import User

MESSAGE_TEXT = (
    "Меню обновлено. Появились новые разделы: «👤 Кабинет» — у всех; "
    "«📊 Статистика» — у руководителей."
)


async def main() -> None:
    redis = Redis(
        host=settings.REDIS_HOST,
        port=settings.REDIS_PORT,
        db=settings.REDIS_DB,
    )
    bot = Bot(
        token=settings.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    sent = 0
    skipped_already = 0
    skipped_forbidden = 0
    skipped_other = 0

    try:
        async with async_session_factory() as session:
            result = await session.execute(
                select(User).where(
                    User.access_status == AccessStatus.APPROVED.value,
                    User.is_active.is_(True),
                )
            )
            users = list(result.scalars().all())

        logger.info(
            "push_menu_update: target MENU_VERSION={} approved+active users={}",
            MENU_VERSION,
            len(users),
        )

        for u in users:
            key = MENU_VERSION_KEY_FMT.format(tg_user_id=u.tg_user_id)
            try:
                current = await redis.get(key)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "redis get failed for tg_id={}: {} — пробуем послать всё равно",
                    u.tg_user_id,
                    exc,
                )
                current = None
            if current is not None:
                value = current.decode() if isinstance(current, bytes) else current
                if value == MENU_VERSION:
                    logger.info("skip tg_id={} (уже на версии {})", u.tg_user_id, value)
                    skipped_already += 1
                    continue

            try:
                role = UserRole(u.role)
                markup = build_main_menu(role)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "skip tg_id={}: build_main_menu/role failed: {}",
                    u.tg_user_id,
                    exc,
                )
                skipped_other += 1
                continue

            try:
                await bot.send_message(
                    chat_id=u.tg_user_id,
                    text=MESSAGE_TEXT,
                    reply_markup=markup,
                )
            except TelegramForbiddenError as exc:
                logger.warning(
                    "tg_id={} заблокировал бота / диалог удалён: {}",
                    u.tg_user_id,
                    exc,
                )
                skipped_forbidden += 1
                continue
            except TelegramRetryAfter as exc:
                wait = exc.retry_after
                logger.warning("flood limit, sleep {}s, retry", wait)
                await asyncio.sleep(wait + 1)
                try:
                    await bot.send_message(
                        chat_id=u.tg_user_id,
                        text=MESSAGE_TEXT,
                        reply_markup=markup,
                    )
                except Exception as exc2:  # noqa: BLE001
                    logger.exception("retry failed for tg_id={}: {}", u.tg_user_id, exc2)
                    skipped_other += 1
                    continue
            except Exception as exc:  # noqa: BLE001
                logger.exception("send failed for tg_id={}: {}", u.tg_user_id, exc)
                skipped_other += 1
                continue

            try:
                await redis.set(key, MENU_VERSION, ex=MENU_VERSION_TTL_S)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "redis set failed for tg_id={}: {} (сообщение УЖЕ ушло)",
                    u.tg_user_id,
                    exc,
                )

            sent += 1
            logger.info("отправлено tg_id={} role={}", u.tg_user_id, u.role)
            # ~20 msg/sec, безопасно ниже Telegram-лимита 30/сек
            await asyncio.sleep(0.05)

        logger.info(
            "push_menu_update готово: отправлено={} уже_на_версии={} заблокировал={} прочие_ошибки={}",
            sent,
            skipped_already,
            skipped_forbidden,
            skipped_other,
        )
    finally:
        await bot.session.close()
        await redis.aclose()


if __name__ == "__main__":
    asyncio.run(main())
