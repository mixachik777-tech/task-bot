"""
Репозиторий истории AI-диалогов.

Контракт:
- Одна активная запись на юзера: при отсутствии — create, иначе используется
  существующая. История хранится как JSONB-массив в `messages`.
- Формат сообщений согласован с google-genai SDK:
  {"role": "user"|"model", "parts": [...]}, где parts — список словарей вида
  {"text": "..."}, {"function_call": {...}}, {"function_response": {...}}.
- trim_to(N) оставляет последние N элементов истории, чтобы JSONB не разрастался.
- clear() обнуляет messages до []; запись сохраняется (счётчик id остаётся).

Методы НЕ коммитят сессию — это забота вызывающей стороны (handler).
"""

from __future__ import annotations

from typing import Any, Sequence

from sqlalchemy import desc, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.db.models import AiConversation


class AiConversationsRepository:
    @staticmethod
    async def get_or_create(
        session: AsyncSession, *, user_id: int
    ) -> AiConversation:
        result = await session.execute(
            select(AiConversation)
            .where(AiConversation.user_id == user_id)
            .order_by(desc(AiConversation.updated_at))
            .limit(1)
        )
        conv = result.scalar_one_or_none()
        if conv is not None:
            return conv
        conv = AiConversation(user_id=user_id, messages=[])
        session.add(conv)
        await session.flush()
        return conv

    @staticmethod
    async def append(
        session: AsyncSession,
        conv: AiConversation,
        message: dict[str, Any],
        *,
        keep_last: int = 20,
    ) -> None:
        """
        Добавляет одно сообщение и при необходимости срезает старые
        до `keep_last`. flag_modified нужен, чтобы SQLAlchemy заметил
        in-place мутацию JSONB-поля.
        """
        msgs = list(conv.messages or [])
        msgs.append(message)
        if len(msgs) > keep_last:
            msgs = msgs[-keep_last:]
        conv.messages = msgs
        flag_modified(conv, "messages")
        await session.flush()

    @staticmethod
    async def extend(
        session: AsyncSession,
        conv: AiConversation,
        messages: Sequence[dict[str, Any]],
        *,
        keep_last: int = 20,
    ) -> None:
        """То же, что append, но для пачки сообщений за один вызов."""
        msgs = list(conv.messages or [])
        msgs.extend(messages)
        if len(msgs) > keep_last:
            msgs = msgs[-keep_last:]
        conv.messages = msgs
        flag_modified(conv, "messages")
        await session.flush()

    @staticmethod
    async def clear(session: AsyncSession, user_id: int) -> None:
        """
        Очищает messages у всех записей юзера (обычно одна).
        Используется кнопкой «🗑 Очистить диалог».
        """
        await session.execute(
            update(AiConversation)
            .where(AiConversation.user_id == user_id)
            .values(messages=[])
        )
