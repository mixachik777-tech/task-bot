"""
Репозиторий пользователей (users).

Контракт: методы НЕ коммитят сессию. Только при необходимости получить
сгенерированный id вызывается session.flush(). Commit/rollback — забота
вызывающей стороны (handler, seeder, скрипт).
"""

from typing import Iterable, Sequence

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.enums import AccessStatus, UserRole
from app.db.models import User


class UsersRepository:
    @staticmethod
    async def get_by_tg_id(session: AsyncSession, tg_user_id: int) -> User | None:
        result = await session.execute(select(User).where(User.tg_user_id == tg_user_id))
        return result.scalar_one_or_none()

    @staticmethod
    async def get_by_id(session: AsyncSession, user_id: int) -> User | None:
        return await session.get(User, user_id)

    @staticmethod
    async def create(
        session: AsyncSession,
        tg_user_id: int,
        full_name: str,
        tg_username: str | None = None,
        role: UserRole = UserRole.EMPLOYEE,
        department_id: int | None = None,
        is_active: bool = False,
    ) -> User:
        user = User(
            tg_user_id=tg_user_id,
            tg_username=tg_username,
            full_name=full_name,
            role=role.value,
            department_id=department_id,
            is_active=is_active,
        )
        session.add(user)
        await session.flush()
        return user

    @staticmethod
    async def update_role(
        session: AsyncSession,
        user_id: int,
        role: UserRole,
        department_id: int | None,
    ) -> None:
        await session.execute(
            update(User)
            .where(User.id == user_id)
            .values(role=role.value, department_id=department_id)
        )

    @staticmethod
    async def set_active(session: AsyncSession, user_id: int, is_active: bool) -> None:
        await session.execute(update(User).where(User.id == user_id).values(is_active=is_active))

    @staticmethod
    async def mark_notified(session: AsyncSession, user_id: int) -> None:
        await session.execute(
            update(User).where(User.id == user_id).values(notified_admins_at=func.now())
        )

    @staticmethod
    async def approve(
        session: AsyncSession,
        user_id: int,
        role: UserRole,
        department_id: int | None,
    ) -> bool:
        """Условный апдейт: меняет роль/отдел/активность только если
        access_status='pending'. Возвращает True, если запись обновлена.
        Защита от гонки двух кликов 'Одобрить' одновременно."""
        result = await session.execute(
            update(User)
            .where(
                User.id == user_id,
                User.access_status == AccessStatus.PENDING.value,
            )
            .values(
                role=role.value,
                department_id=department_id,
                is_active=True,
                access_status=AccessStatus.APPROVED.value,
            )
        )
        return result.rowcount > 0

    @staticmethod
    async def deny(session: AsyncSession, user_id: int) -> bool:
        """Условный апдейт: пометить отклонённым только если pending.
        Возвращает True при успехе."""
        result = await session.execute(
            update(User)
            .where(
                User.id == user_id,
                User.access_status == AccessStatus.PENDING.value,
            )
            .values(
                is_active=False,
                access_status=AccessStatus.DENIED.value,
            )
        )
        return result.rowcount > 0

    @staticmethod
    async def reset_to_pending(session: AsyncSession, user_id: int) -> bool:
        """Условный апдейт: вернуть отклонённого юзера в pending, чтобы
        он мог повторно подать заявку. Обнуляем notified_admins_at, чтобы
        следующий шаг отправил аппруверу новое уведомление.
        Возвращает True, если запись обновлена."""
        result = await session.execute(
            update(User)
            .where(
                User.id == user_id,
                User.access_status == AccessStatus.DENIED.value,
            )
            .values(
                access_status=AccessStatus.PENDING.value,
                notified_admins_at=None,
            )
        )
        return result.rowcount > 0

    @staticmethod
    async def update_department(
        session: AsyncSession, user_id: int, department_id: int | None
    ) -> None:
        """
        Меняет только отдел, не трогая роль (в отличие от update_role).
        Нужен для раздела «👥 Состав», где admin таскает людей по отделам.
        """
        await session.execute(
            update(User).where(User.id == user_id).values(department_id=department_id)
        )

    @staticmethod
    async def list_active(session: AsyncSession) -> Sequence[User]:
        result = await session.execute(
            select(User).where(User.is_active.is_(True)).order_by(User.id)
        )
        return result.scalars().all()

    @staticmethod
    async def list_pending_approval(session: AsyncSession) -> Sequence[User]:
        result = await session.execute(
            select(User).where(User.is_active.is_(False)).order_by(User.id)
        )
        return result.scalars().all()

    @staticmethod
    async def list_by_department(session: AsyncSession, department_id: int) -> Sequence[User]:
        result = await session.execute(
            select(User).where(User.department_id == department_id).order_by(User.id)
        )
        return result.scalars().all()

    @staticmethod
    async def list_approved_in_department(
        session: AsyncSession,
        *,
        department_id: int,
        exclude_user_id: int | None = None,
    ) -> Sequence[User]:
        """Активные approved-сотрудники отдела.

        Используется как набор кандидатов для передачи задачи (reassign):
        исполнитель видит коллег по отделу, кому может перекинуть.
        exclude_user_id — обычно текущий assignee, нет смысла передавать
        задачу самому себе.
        """
        stmt = select(User).where(
            User.department_id == department_id,
            User.is_active.is_(True),
            User.access_status == "approved",
        )
        if exclude_user_id is not None:
            stmt = stmt.where(User.id != exclude_user_id)
        stmt = stmt.order_by(User.full_name.asc(), User.id.asc())
        result = await session.execute(stmt)
        return result.scalars().all()

    @staticmethod
    async def list_active_with_department(
        session: AsyncSession,
    ) -> Sequence[User]:
        """
        Все активные юзеры с подгруженным `department`. Сортировка:
        nulls last по department_id (без отдела — в конце), внутри отдела по
        полному имени.
        """
        result = await session.execute(
            select(User)
            .options(selectinload(User.department))
            .where(User.is_active.is_(True))
            .order_by(User.department_id.asc().nulls_last(), User.full_name.asc())
        )
        return result.scalars().all()

    @staticmethod
    async def list_by_ids(session: AsyncSession, ids: Iterable[int]) -> Sequence[User]:
        id_list = list(ids)
        if not id_list:
            return []
        result = await session.execute(
            select(User).where(User.id.in_(id_list)).order_by(User.full_name)
        )
        return result.scalars().all()
