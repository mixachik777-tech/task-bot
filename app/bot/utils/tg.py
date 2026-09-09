"""
Безопасные обёртки над Telegram Bot API.

Все TG-вызовы из сервисов проходят через эти функции:
- send_with_retry — send_message с ретраями на TelegramRetryAfter (3 попытки)
- edit_text_safe  — edit_message_text с обработкой 'not modified', 'message gone' + ретраи
- send_dm_safe    — send_message в личку, best-effort (False = не доставлено)

Контракт edit_text_safe:
- True  → сообщение реально обновлено
- False → 'not modified' / 'message to edit not found' / Forbidden / другая BadRequest
- raise → исчерпан retry на flood, или системная ошибка
"""

import asyncio

from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.types import InlineKeyboardMarkup, Message
from loguru import logger

MAX_TG_RETRIES = 3


def _is_not_modified(exc: TelegramBadRequest) -> bool:
    return "not modified" in str(exc).lower()


def _is_message_gone(exc: TelegramBadRequest) -> bool:
    s = str(exc).lower()
    return "message to edit not found" in s or "message can't be edited" in s


async def send_with_retry(
    bot: Bot,
    *,
    chat_id: int,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    message_thread_id: int | None = None,
    reply_to_message_id: int | None = None,
) -> Message:
    """send_message с ретраями на TelegramRetryAfter. Прочие ошибки пробрасываются."""
    last_exc: Exception | None = None
    for attempt in range(MAX_TG_RETRIES):
        try:
            return await bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=reply_markup,
                message_thread_id=message_thread_id,
                reply_to_message_id=reply_to_message_id,
            )
        except TelegramRetryAfter as exc:
            last_exc = exc
            logger.warning(
                "send_message flood (attempt {}/{}): retry_after={}s",
                attempt + 1,
                MAX_TG_RETRIES,
                exc.retry_after,
            )
            await asyncio.sleep(exc.retry_after)
    assert last_exc is not None
    raise last_exc


async def edit_text_safe(
    bot: Bot,
    *,
    chat_id: int,
    message_id: int,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> bool:
    """
    edit_message_text с retry на flood и проглатыванием 'мягких' ошибок.

    True  → реально обновили.
    False → 'not modified' (текст идентичен) или 'message to edit not found'
            (сообщение удалено) или Forbidden (нет доступа). Не падаем.
    raise → исчерпан retry на flood (TelegramRetryAfter ×3) или
            неожиданная BadRequest, не относящаяся к 'мягким'.
    """
    last_flood: Exception | None = None
    for attempt in range(MAX_TG_RETRIES):
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                reply_markup=reply_markup,
            )
            return True
        except TelegramRetryAfter as exc:
            last_flood = exc
            logger.warning(
                "edit_message flood (attempt {}/{}): retry_after={}s",
                attempt + 1,
                MAX_TG_RETRIES,
                exc.retry_after,
            )
            await asyncio.sleep(exc.retry_after)
        except TelegramBadRequest as exc:
            if _is_not_modified(exc):
                logger.debug("edit_message: not modified — skipped")
                return False
            if _is_message_gone(exc):
                logger.warning(
                    "edit_message: target gone (chat={}, msg={})",
                    chat_id,
                    message_id,
                )
                return False
            logger.error(
                "edit_message BadRequest (chat={}, msg={}): {}",
                chat_id,
                message_id,
                exc,
            )
            raise
        except TelegramForbiddenError as exc:
            logger.error(
                "edit_message Forbidden (chat={}, msg={}): {}",
                chat_id,
                message_id,
                exc,
            )
            return False
    assert last_flood is not None
    raise last_flood


async def send_file_with_retry(
    bot: Bot,
    *,
    chat_id: int,
    file_id: str,
    kind: str = "document",
    file_name: str | None = None,
    caption: str | None = None,
    message_thread_id: int | None = None,
    reply_markup: InlineKeyboardMarkup | None = None,
    reply_to_message_id: int | None = None,
) -> Message:
    """
    Отправка вложения через метод, соответствующий типу:
      photo     → send_photo
      video     → send_video
      animation → send_animation
      document  → send_document
    Несовпадение типа и file_id у Telegram даёт BadRequest 'can't use file of
    type X as Y' — поэтому диспетчеризация обязательна.

    Ретрай только на TelegramRetryAfter (3 попытки), прочие ошибки наружу.
    """

    async def _call() -> Message:
        if kind == "photo":
            return await bot.send_photo(
                chat_id=chat_id,
                photo=file_id,
                caption=caption,
                message_thread_id=message_thread_id,
                reply_markup=reply_markup,
                reply_to_message_id=reply_to_message_id,
            )
        if kind == "video":
            return await bot.send_video(
                chat_id=chat_id,
                video=file_id,
                caption=caption,
                message_thread_id=message_thread_id,
                reply_markup=reply_markup,
                reply_to_message_id=reply_to_message_id,
            )
        if kind == "animation":
            return await bot.send_animation(
                chat_id=chat_id,
                animation=file_id,
                caption=caption,
                message_thread_id=message_thread_id,
                reply_markup=reply_markup,
                reply_to_message_id=reply_to_message_id,
            )
        return await bot.send_document(
            chat_id=chat_id,
            document=file_id,
            caption=caption,
            message_thread_id=message_thread_id,
            reply_markup=reply_markup,
            reply_to_message_id=reply_to_message_id,
        )

    last_exc: Exception | None = None
    for attempt in range(MAX_TG_RETRIES):
        try:
            return await _call()
        except TelegramRetryAfter as exc:
            last_exc = exc
            logger.warning(
                "send_file flood (attempt {}/{}): retry_after={}s kind={} file_name={!r}",
                attempt + 1,
                MAX_TG_RETRIES,
                exc.retry_after,
                kind,
                file_name,
            )
            await asyncio.sleep(exc.retry_after)
    assert last_exc is not None
    raise last_exc


def build_topic_message_link(chat_id: int, topic_id: int | None, message_id: int) -> str | None:
    """
    Глубокая ссылка на сообщение в супергруппе/forum-топике.

    Формат: `https://t.me/c/<chat_id_clean>/<topic_id>/<message_id>`.
    Для супергрупп Telegram использует chat_id без префикса `-100`.
    Если group без forum-топиков (topic_id is None или 0), формат
    `https://t.me/c/<chat>/<msg>`.

    Возвращает None для невалидных входов (например, chat_id=0).
    """
    if not chat_id or not message_id:
        return None
    raw = str(chat_id)
    if raw.startswith("-100"):
        raw = raw[4:]
    elif raw.startswith("-"):
        raw = raw[1:]
    if not raw.isdigit():
        return None
    if topic_id:
        return f"https://t.me/c/{raw}/{topic_id}/{message_id}"
    return f"https://t.me/c/{raw}/{message_id}"


async def send_dm_safe(
    bot: Bot,
    *,
    chat_id: int,
    text: str,
    disable_link_preview: bool = False,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> bool:
    """
    Уведомление в личку. Лучше всего отдаёт; если юзер не писал боту /start —
    Telegram отдаст BadRequest/Forbidden. Возвращает False, не падает.

    `disable_link_preview=True` отключает превью первой ссылки в тексте —
    нужно, например, для overdue-уведомлений со ссылкой на сообщение
    в чате, чтобы preview не занимало половину DM.
    """
    from aiogram.types import LinkPreviewOptions

    kwargs: dict = {"chat_id": chat_id, "text": text}
    if disable_link_preview:
        kwargs["link_preview_options"] = LinkPreviewOptions(is_disabled=True)
    if reply_markup is not None:
        kwargs["reply_markup"] = reply_markup
    for attempt in range(2):  # 1 повтор после flood-сна
        try:
            await bot.send_message(**kwargs)
            return True
        except (TelegramBadRequest, TelegramForbiddenError) as exc:
            logger.warning("DM to {} failed: {}", chat_id, exc)
            return False
        except TelegramRetryAfter as exc:
            if attempt == 0:
                logger.warning(
                    "DM to {} hit flood, sleeping {}s and retrying once",
                    chat_id,
                    exc.retry_after,
                )
                await asyncio.sleep(exc.retry_after)
                continue
            logger.warning("DM to {} still flooded after retry — отложено", chat_id)
            return False
    return False
