"""
Репозиторий task_broadcasts — карточки задачи в личке участников
направления (Этап А «Бот без чата»).

Контракт:
- `add` — INSERT с ON CONFLICT DO NOTHING. Идемпотентен, повторный
  broadcast-attempt'а одной и той же задачи не плодит дубли.
- `list_by_task` — все доставки задачи (для accept-cleanup и аудита).
- `delete_others` — удалить доставки задачи всем кроме принявшего;
  возвращает удалённые строки (нужны для bot.delete_message в личке).
- Сессия не коммитится здесь.
"""

from __future__ import annotations

from typing import Sequence

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import TaskBroadcast


class BroadcastsRepository:
    @staticmethod
    async def add(
        session: AsyncSession,
        *,
        task_id: int,
        user_id: int,
        tg_user_id: int,
        message_id: int,
    ) -> bool:
        """
        Регистрирует доставку. True — добавили, False — уже была (idempotent).
        UNIQUE (task_id, user_id) защищает от дублей при ретраях outbox.
        """
        stmt = (
            pg_insert(TaskBroadcast)
            .values(
                task_id=task_id,
                user_id=user_id,
                tg_user_id=tg_user_id,
                message_id=message_id,
            )
            .on_conflict_do_nothing(constraint="uq_task_broadcasts_task_user")
            .returning(TaskBroadcast.id)
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none() is not None

    @staticmethod
    async def list_by_task(session: AsyncSession, task_id: int) -> Sequence[TaskBroadcast]:
        result = await session.execute(
            select(TaskBroadcast).where(TaskBroadcast.task_id == task_id)
        )
        return result.scalars().all()

    @staticmethod
    async def delete_others(
        session: AsyncSession, *, task_id: int, keep_user_id: int
    ) -> list[TaskBroadcast]:
        """
        Удаляет все доставки задачи кроме keep_user_id (принявшего).
        Возвращает удалённые строки — caller'у нужны tg_user_id+message_id
        для bot.delete_message в личке. Сессия не коммитится здесь.
        """
        result = await session.execute(
            select(TaskBroadcast).where(
                TaskBroadcast.task_id == task_id,
                TaskBroadcast.user_id != keep_user_id,
            )
        )
        rows = list(result.scalars().all())
        if rows:
            await session.execute(
                delete(TaskBroadcast).where(
                    TaskBroadcast.task_id == task_id,
                    TaskBroadcast.user_id != keep_user_id,
                )
            )
        return rows

    @staticmethod
    async def get_for_user(
        session: AsyncSession, *, task_id: int, user_id: int
    ) -> TaskBroadcast | None:
        result = await session.execute(
            select(TaskBroadcast).where(
                TaskBroadcast.task_id == task_id,
                TaskBroadcast.user_id == user_id,
            )
        )
        return result.scalar_one_or_none()
