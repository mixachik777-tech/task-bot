"""
Извлечение файлов из Message / списка Message (альбом).

Два варианта file-dict живут в проекте исторически:
- "full" (create_task): kind, file_id, unique_id, file_name, size, mime_type
- "short" (task_actions, task_question): kind, file_id, file_name

Чтобы не ломать существующие consumer'ы (рендер карточки/outbox), оба
shape сохранены. Дифф между ними — только в дополнительных полях.

Все extractor'ы возвращают ОДИН file_dict из ОДНОГО Message. Батчинг
делает caller (handler) по списку из data["album"].
"""

from __future__ import annotations

from typing import Any

from aiogram.types import Message

# Telegram Bot API не пропускает файлы крупнее 20 МБ при отправке через бота.
MAX_FILE_SIZE = 20 * 1024 * 1024


def file_full_from_message(message: Message) -> dict[str, Any] | None:
    """Полный shape — для create_task FSM (task_files модель ждёт mime/size)."""
    d = message.document
    if d is not None:
        return {
            "file_id": d.file_id,
            "unique_id": d.file_unique_id,
            "file_name": d.file_name,
            "size": d.file_size,
            "mime_type": d.mime_type,
            "kind": "document",
        }
    if message.photo:
        p = message.photo[-1]
        return {
            "file_id": p.file_id,
            "unique_id": p.file_unique_id,
            "file_name": None,
            "size": p.file_size,
            "mime_type": "image/jpeg",
            "kind": "photo",
        }
    v = message.video
    if v is not None:
        return {
            "file_id": v.file_id,
            "unique_id": v.file_unique_id,
            "file_name": v.file_name,
            "size": v.file_size,
            "mime_type": v.mime_type or "video/mp4",
            "kind": "video",
        }
    a = message.animation
    if a is not None:
        return {
            "file_id": a.file_id,
            "unique_id": a.file_unique_id,
            "file_name": a.file_name,
            "size": a.file_size,
            "mime_type": a.mime_type or "image/gif",
            "kind": "animation",
        }
    return None


def file_short_from_message(message: Message) -> dict[str, Any] | None:
    """Короткий shape — для task_actions/task_question (рендер по file_id + kind)."""
    if message.document is not None:
        return {
            "kind": "document",
            "file_id": message.document.file_id,
            "file_name": message.document.file_name,
        }
    if message.photo:
        return {
            "kind": "photo",
            "file_id": message.photo[-1].file_id,
            "file_name": None,
        }
    if message.video is not None:
        return {
            "kind": "video",
            "file_id": message.video.file_id,
            "file_name": message.video.file_name,
        }
    if message.animation is not None:
        return {
            "kind": "animation",
            "file_id": message.animation.file_id,
            "file_name": message.animation.file_name,
        }
    return None


def file_size_for_message(message: Message) -> int | None:
    """Возвращает размер вложения в байтах (или None если Telegram не сообщил)."""
    if message.document is not None:
        return message.document.file_size
    if message.photo:
        return message.photo[-1].file_size
    if message.video is not None:
        return message.video.file_size
    if message.animation is not None:
        return message.animation.file_size
    return None


def collect_files_from_messages(
    messages: list[Message],
    *,
    extractor,
    current_count: int,
    max_count: int,
) -> tuple[list[dict[str, Any]], int, int]:
    """
    Прогоняет список сообщений через extractor с учётом лимитов.

    Возвращает:
    - accepted: список новых file_dict'ов, помещающихся в лимит
    - rejected_oversize: сколько отброшено по причине size > 20MB
    - rejected_overflow: сколько отброшено по причине превышения max_count

    Сообщения, из которых extractor вернул None (не вложение), игнорируются
    молча — это либо текст с подписью без файла, либо неподдерживаемый тип.
    """
    accepted: list[dict[str, Any]] = []
    rejected_oversize = 0
    rejected_overflow = 0
    remaining = max_count - current_count
    for msg in messages:
        size = file_size_for_message(msg)
        if size is not None and size > MAX_FILE_SIZE:
            rejected_oversize += 1
            continue
        f = extractor(msg)
        if f is None:
            continue
        if remaining <= 0:
            rejected_overflow += 1
            continue
        accepted.append(f)
        remaining -= 1
    return accepted, rejected_oversize, rejected_overflow
