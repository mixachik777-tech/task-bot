"""
Репозиторий отделов (departments).

Контракт: методы НЕ коммитят сессию. Только при необходимости получить
сгенерированный id вызывается session.flush(). Commit/rollback — забота
вызывающей стороны.
"""

from typing import Sequence

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Department


class DepartmentsRepository:
    @staticmethod
    async def get_by_code(session: AsyncSession, code: str) -> Department | None:
        result = await session.execute(
            select(Department).where(Department.code == code)
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def get_by_id(
        session: AsyncSession, department_id: int
    ) -> Department | None:
        return await session.get(Department, department_id)

    @staticmethod
    async def list_active(session: AsyncSession) -> Sequence[Department]:
        result = await session.execute(
            select(Department)
            .where(Department.is_active.is_(True))
            .order_by(Department.name)
        )
        return result.scalars().all()

    @staticmethod
    async def create(
        session: AsyncSession,
        code: str,
        name: str,
        topic_id: int = 0,
    ) -> Department:
        dept = Department(code=code, name=name, topic_id=topic_id)
        session.add(dept)
        await session.flush()
        return dept

    @staticmethod
    async def update_topic_id(
        session: AsyncSession, department_id: int, topic_id: int
    ) -> None:
        await session.execute(
            update(Department)
            .where(Department.id == department_id)
            .values(topic_id=topic_id)
        )
