"""Репозиторий task_outbox — операции для outbox-worker'а."""

from datetime import datetime, timedelta, timezone
from typing import Sequence

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import TaskOutbox


class OutboxRepository:
    @staticmethod
    async def add(session: AsyncSession, task_id: int, action: str) -> TaskOutbox:
        row = TaskOutbox(task_id=task_id, action=action)
        session.add(row)
        await session.flush()
        return row

    @staticmethod
    async def enqueue_idempotent(session: AsyncSession, task_id: int, action: str) -> None:
        """Атомарно поставить в очередь, no-op если pending-запись уже есть.

        Использует ON CONFLICT DO NOTHING по uq_outbox_task_action_active
        (partial unique по (task_id, action) WHERE status='pending').
        Postgres требует ТОЧНОГО совпадения предиката index_where с
        предикатом индекса — иначе ProgrammingError 'no unique or
        exclusion constraint matching the ON CONFLICT specification'.
        Миграция g5h6i7j8k9l0 поменяла индекс на pending-only (чтобы
        повторный update_lead после done не блокировался), код тогда
        синхронно обновлён не был — лечим здесь.

        Безопасно вызывать внутри основной транзакции изменения статуса
        задачи: либо мы поставили в очередь, либо она уже там — в любом
        случае воркер дожмёт. Никаких силовых перехватов UniqueViolation."""
        stmt = (
            pg_insert(TaskOutbox)
            .values(task_id=task_id, action=action)
            .on_conflict_do_nothing(
                index_elements=["task_id", "action"],
                index_where=text("status::text = 'pending'::text"),
            )
        )
        await session.execute(stmt)

    @staticmethod
    async def lock_due_pending(session: AsyncSession, *, limit: int = 50) -> Sequence[TaskOutbox]:
        """SELECT … FOR UPDATE SKIP LOCKED — выдаёт записи, готовые к
        retry. SKIP LOCKED исключает дублирующую обработку, если кто-то
        другой уже взял запись в работу."""
        now = datetime.now(timezone.utc)
        stmt = (
            select(TaskOutbox)
            .where(
                TaskOutbox.status == "pending",
                TaskOutbox.next_retry_at <= now,
            )
            .order_by(TaskOutbox.next_retry_at.asc(), TaskOutbox.id.asc())
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        result = await session.execute(stmt)
        return result.scalars().all()

    @staticmethod
    async def lock_pending_for_task(session: AsyncSession, task_id: int) -> Sequence[TaskOutbox]:
        """Все pending-записи конкретной задачи, под FOR UPDATE."""
        stmt = (
            select(TaskOutbox)
            .where(
                TaskOutbox.task_id == task_id,
                TaskOutbox.status == "pending",
            )
            .order_by(TaskOutbox.id.asc())
            .with_for_update(skip_locked=True)
        )
        result = await session.execute(stmt)
        return result.scalars().all()

    @staticmethod
    async def mark_done(session: AsyncSession, outbox_id: int) -> None:
        await session.execute(
            update(TaskOutbox)
            .where(TaskOutbox.id == outbox_id)
            .values(status="done", last_error=None)
        )

    @staticmethod
    async def reschedule(
        session: AsyncSession,
        outbox_id: int,
        *,
        delay: timedelta,
        error: str,
        attempts: int,
    ) -> None:
        next_at = datetime.now(timezone.utc) + delay
        await session.execute(
            update(TaskOutbox)
            .where(TaskOutbox.id == outbox_id)
            .values(
                attempts=attempts,
                last_error=error[:1000],
                next_retry_at=next_at,
            )
        )

    @staticmethod
    async def mark_failed(
        session: AsyncSession, outbox_id: int, *, error: str, attempts: int
    ) -> None:
        await session.execute(
            update(TaskOutbox)
            .where(TaskOutbox.id == outbox_id)
            .values(
                status="failed",
                attempts=attempts,
                last_error=error[:1000],
            )
        )
