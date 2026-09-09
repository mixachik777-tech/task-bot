"""Системные алерты владельцу бота (Михаилу).

Адресат — `settings.ADMIN_TG_ID` (личный чат пользователя с ботом).
Никакие алерты не уходят рядовым юзерам — у них только нейтральное
«Ошибка загрузки, попробуйте позже или обратитесь к разработчику».

`send_alert` — best-effort с одним retry на flood. `dispatch_alert` —
безопасная обёртка для fire-and-forget (`asyncio.create_task`): никогда
не пробрасывает исключения, всегда логирует факт потери. Это критично:
если алерт потеряется молча, мы не узнаем, что в системе проблема.
"""

from __future__ import annotations

import asyncio

from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from loguru import logger

from app.config import settings


async def send_alert(bot: Bot, text: str) -> bool:
    """Отправить системный алерт Михаилу. True — если доставлено."""
    chat_id = settings.ADMIN_TG_ID
    for attempt in range(2):
        try:
            await bot.send_message(chat_id=chat_id, text=text)
            return True
        except TelegramRetryAfter as exc:
            if attempt == 0:
                logger.warning(
                    "system alert flood, sleeping {}s and retrying once",
                    exc.retry_after,
                )
                await asyncio.sleep(exc.retry_after)
                continue
            logger.error(
                "system alert lost after flood-retry | text={!r}", text
            )
            return False
        except (TelegramBadRequest, TelegramForbiddenError) as exc:
            logger.error(
                "system alert delivery failed (perm): {} | text={!r}",
                exc, text,
            )
            return False
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "system alert delivery crashed: {} | text={!r}", exc, text
            )
            return False
    return False


def dispatch_alert(bot: Bot, text: str) -> asyncio.Task:
    """Безопасная обёртка под `asyncio.create_task`. Никогда не пробрасывает
    исключения, всегда возвращает Task. Использовать из background-job'ов,
    где упавшая отправка алерта не должна сломать сам job."""

    async def _run() -> None:
        try:
            await send_alert(bot, text)
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "dispatch_alert outer failure: {} | text={!r}", exc, text
            )

    return asyncio.create_task(_run())
