"""
Репозиторий событий задач (task_history).

Все статусные переходы и комментарии задач логируются сюда.
Сортировка по времени всегда с тай-брейкером по id (см. BACKLOG).
"""

from typing import Any, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import HistoryEventType
from app.db.models import TaskHistory


class HistoryRepository:
    @staticmethod
    async def log(
        session: AsyncSession,
        *,
        task_id: int,
        user_id: int | None,
        event_type: HistoryEventType,
        payload: dict[str, Any] | None = None,
    ) -> TaskHistory:
        entry = TaskHistory(
            task_id=task_id,
            user_id=user_id,
            event_type=event_type.value,
            payload=payload,
        )
        session.add(entry)
        await session.flush()
        return entry

    @staticmethod
    async def list_by_task(
        session: AsyncSession, task_id: int
    ) -> Sequence[TaskHistory]:
        result = await session.execute(
            select(TaskHistory)
            .where(TaskHistory.task_id == task_id)
            .order_by(TaskHistory.created_at.asc(), TaskHistory.id.asc())
        )
        return result.scalars().all()

    @staticmethod
    async def list_by_user_period(
        session: AsyncSession,
        *,
        user_id: int,
        date_from,
        date_to,
        limit: int = 50,
    ) -> Sequence[TaskHistory]:
        """
        События задач, инициированные конкретным юзером в окне [from, to).
        Сортировка: created_at ASC + id ASC (стабильный тай-брейкер).
        Используется AI-инструментом get_user_history.
        """
        result = await session.execute(
            select(TaskHistory)
            .where(
                TaskHistory.user_id == user_id,
                TaskHistory.created_at >= date_from,
                TaskHistory.created_at < date_to,
            )
            .order_by(TaskHistory.created_at.asc(), TaskHistory.id.asc())
            .limit(limit)
        )
        return result.scalars().all()
