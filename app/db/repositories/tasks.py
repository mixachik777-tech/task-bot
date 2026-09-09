"""
Репозиторий задач (tasks) и связанных файлов (task_files).

Контракт: методы НЕ коммитят сессию. Только flush после add для получения id.
"""

from datetime import datetime, timezone
from typing import Any, Sequence

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.enums import TaskPriority, TaskStatus
from app.db.models import Task, TaskFile

ARCHIVE_STATUSES = (TaskStatus.DONE.value, TaskStatus.CANCELLED.value)


class TasksRepository:
    @staticmethod
    async def _next_display_number(session: AsyncSession) -> int:
        """Следующий человеческий номер задачи.

        Берём MAX(display_number) с pg_advisory_xact_lock, чтобы две
        параллельные транзакции не получили одинаковое значение. Лок
        отпускается с концом транзакции, без отдельной sequence.
        Ключ лока (8127342819 — случайный bigint, постоянный).
        """
        # advisory-lock в рамках транзакции; гарантирует сериализацию
        # без блокировки строк таблицы tasks.
        await session.execute(select(func.pg_advisory_xact_lock(8127342819)))
        row = await session.execute(select(func.max(Task.display_number)))
        cur = row.scalar() or 0
        return int(cur) + 1

    @staticmethod
    async def create(
        session: AsyncSession,
        *,
        title: str,
        description: str | None,
        priority: TaskPriority,
        deadline: datetime,
        creator_id: int,
        department_id: int,
    ) -> Task:
        display_number = await TasksRepository._next_display_number(session)
        task = Task(
            display_number=display_number,
            title=title,
            description=description,
            priority=priority.value,
            deadline=deadline,
            creator_id=creator_id,
            department_id=department_id,
        )
        session.add(task)
        await session.flush()
        return task

    @staticmethod
    async def get_by_id(session: AsyncSession, task_id: int) -> Task | None:
        return await session.get(Task, task_id)

    @staticmethod
    async def get_display_number(session: AsyncSession, task_id: int) -> int | None:
        """Лёгкий lookup человеческого номера задачи по внутреннему id.

        Используется в handler'ах, у которых на руках только task_id из
        callback_data, чтобы сформировать user-facing текст с #display_number,
        а не с #id (id может содержать дыры).
        """
        r = await session.execute(select(Task.display_number).where(Task.id == task_id))
        v = r.scalar_one_or_none()
        return int(v) if v is not None else None

    @staticmethod
    async def lock_for_update(session: AsyncSession, task_id: int) -> Task | None:
        """
        SELECT … FOR UPDATE на одной строке tasks. Удерживает лок до конца
        транзакции — защита от двойного клика «Принять» и параллельных
        смен статуса. Если строки нет, возвращает None.
        """
        result = await session.execute(select(Task).where(Task.id == task_id).with_for_update())
        return result.scalar_one_or_none()

    @staticmethod
    async def count_files(session: AsyncSession, task_id: int) -> int:
        result = await session.execute(
            select(func.count()).select_from(TaskFile).where(TaskFile.task_id == task_id)
        )
        return int(result.scalar_one())

    @staticmethod
    async def get_with_files(session: AsyncSession, task_id: int) -> Task | None:
        result = await session.execute(
            select(Task).options(selectinload(Task.files)).where(Task.id == task_id)
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def add_files(
        session: AsyncSession,
        task_id: int,
        files: Sequence[dict[str, Any]],
        *,
        purpose: str = "creation",
    ) -> None:
        for f in files:
            session.add(
                TaskFile(
                    task_id=task_id,
                    tg_file_id=f["file_id"],
                    tg_file_unique_id=f.get("unique_id"),
                    file_name=f.get("file_name"),
                    file_size=f.get("size"),
                    mime_type=f.get("mime_type"),
                    kind=f.get("kind", "document"),
                    purpose=purpose,
                )
            )
        await session.flush()

    @staticmethod
    async def set_message_ids(
        session: AsyncSession,
        task_id: int,
        *,
        dept_chat_id: int | None = None,
        dept_message_id: int | None = None,
        arch_message_id: int | None = None,
    ) -> None:
        values: dict[str, Any] = {}
        if dept_chat_id is not None:
            values["dept_chat_id"] = dept_chat_id
        if dept_message_id is not None:
            values["dept_message_id"] = dept_message_id
        if arch_message_id is not None:
            values["arch_message_id"] = arch_message_id
        if not values:
            return
        await session.execute(update(Task).where(Task.id == task_id).values(**values))

    @staticmethod
    async def list_active(
        session: AsyncSession,
        *,
        department_id: int | None = None,
        assignee_id: int | None = None,
    ) -> Sequence[Task]:
        stmt = (
            select(Task)
            .where(Task.status.in_([TaskStatus.NEW.value, TaskStatus.IN_PROGRESS.value]))
            .order_by(Task.deadline.asc(), Task.id.asc())
        )
        if department_id is not None:
            stmt = stmt.where(Task.department_id == department_id)
        if assignee_id is not None:
            stmt = stmt.where(Task.assignee_id == assignee_id)
        result = await session.execute(stmt)
        return result.scalars().all()

    @staticmethod
    def _archive_filters(
        *,
        department_id: int | None,
        assignee_id: int | None,
        status: str | None,
        date_from: datetime | None,
        date_to: datetime | None,
    ) -> list[Any]:
        """
        Сборка WHERE-условий для архивных запросов.

        status=None → все архивные статусы (done, cancelled).
        Сортировка по completed_at для done; для cancelled completed_at может
        быть NULL, тогда падаем на created_at — поэтому фильтрация по дате
        идёт по COALESCE(completed_at, created_at).
        """
        conds: list[Any] = []
        if status is None:
            conds.append(Task.status.in_(ARCHIVE_STATUSES))
        else:
            conds.append(Task.status == status)
        if department_id is not None:
            conds.append(Task.department_id == department_id)
        if assignee_id is not None:
            conds.append(Task.assignee_id == assignee_id)
        if date_from is not None:
            conds.append(func.coalesce(Task.completed_at, Task.created_at) >= date_from)
        if date_to is not None:
            conds.append(func.coalesce(Task.completed_at, Task.created_at) < date_to)
        return conds

    @staticmethod
    async def list_archive(
        session: AsyncSession,
        *,
        department_id: int | None = None,
        assignee_id: int | None = None,
        status: str | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        limit: int = 10,
        offset: int = 0,
    ) -> Sequence[Task]:
        """
        Пагинированный список архивных задач (done/cancelled).

        Сортировка: COALESCE(completed_at, created_at) DESC, id DESC — стабильный
        тай-брейкер.
        """
        conds = TasksRepository._archive_filters(
            department_id=department_id,
            assignee_id=assignee_id,
            status=status,
            date_from=date_from,
            date_to=date_to,
        )
        order_col = func.coalesce(Task.completed_at, Task.created_at)
        stmt = (
            select(Task)
            .where(*conds)
            .order_by(order_col.desc(), Task.id.desc())
            .limit(limit)
            .offset(offset)
        )
        result = await session.execute(stmt)
        return result.scalars().all()

    @staticmethod
    async def count_archive(
        session: AsyncSession,
        *,
        department_id: int | None = None,
        assignee_id: int | None = None,
        status: str | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
    ) -> int:
        conds = TasksRepository._archive_filters(
            department_id=department_id,
            assignee_id=assignee_id,
            status=status,
            date_from=date_from,
            date_to=date_to,
        )
        stmt = select(func.count()).select_from(Task).where(*conds)
        result = await session.execute(stmt)
        return int(result.scalar_one())

    @staticmethod
    async def get_full(session: AsyncSession, task_id: int) -> Task | None:
        """
        Полная задача: + creator, assignee, department, files. Для рендера
        детальной карточки в архиве и в AI-инструменте `get_task_details`.
        """
        result = await session.execute(
            select(Task)
            .options(
                selectinload(Task.creator),
                selectinload(Task.assignee),
                selectinload(Task.department),
                selectinload(Task.files),
            )
            .where(Task.id == task_id)
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def list_unassigned(
        session: AsyncSession,
        *,
        department_id: int | None = None,
        limit: int = 50,
    ) -> Sequence[Task]:
        """
        Свободные задачи: status='new' AND assignee_id IS NULL.
        Любой одобренный сотрудник может взять их через кнопку «Принять»
        в топике отдела. Используется блоком «🎯 Свободные» в личном кабинете.

        Сортировка: deadline ASC + id ASC. department_id опциональный —
        в кабинете показываем по всем отделам (топики открыты всем).
        """
        stmt = (
            select(Task)
            .options(selectinload(Task.department))
            .where(
                Task.status == TaskStatus.NEW.value,
                Task.assignee_id.is_(None),
            )
            .order_by(Task.deadline.asc(), Task.id.asc())
            .limit(limit)
        )
        if department_id is not None:
            stmt = stmt.where(Task.department_id == department_id)
        result = await session.execute(stmt)
        return result.scalars().all()

    @staticmethod
    async def list_for_admin_archive(
        session: AsyncSession,
        *,
        limit: int,
        offset: int,
    ) -> tuple[Sequence[Task], int]:
        """Лента «Все задачи» для админского кабинета.

        Возвращает (страница задач, total_count). Сортировка — created_at DESC,
        id DESC: новые сверху. Грузим creator/assignee/department/files одним
        запросом (selectinload), иначе на 20 задачах N+1 N=20.
        """
        from sqlalchemy import func

        total_q = select(func.count()).select_from(Task)
        total = int((await session.execute(total_q)).scalar_one())

        stmt = (
            select(Task)
            .options(
                selectinload(Task.creator),
                selectinload(Task.assignee),
                selectinload(Task.department),
                selectinload(Task.files),
            )
            .order_by(Task.created_at.desc(), Task.id.desc())
            .offset(offset)
            .limit(limit)
        )
        result = await session.execute(stmt)
        return result.scalars().all(), total

    @staticmethod
    async def list_active_for_user(
        session: AsyncSession,
        *,
        user_id: int,
        limit: int = 50,
    ) -> Sequence[Task]:
        """
        Активные задачи юзера в широком смысле — для меню «🔎 Открыть карточку»
        в кабинете (Этап Г):
          - где он assignee и status in (new, in_progress, awaiting_approval);
          - и где он creator, задача ещё не принята (status=new AND assignee=NULL).

        Сортировка: deadline ASC + id ASC.
        """
        from sqlalchemy import or_, and_

        stmt = (
            select(Task)
            .where(
                or_(
                    and_(
                        Task.assignee_id == user_id,
                        Task.status.in_(
                            [
                                TaskStatus.NEW.value,
                                TaskStatus.IN_PROGRESS.value,
                                TaskStatus.AWAITING_APPROVAL.value,
                            ]
                        ),
                    ),
                    and_(
                        Task.creator_id == user_id,
                        Task.assignee_id.is_(None),
                        Task.status == TaskStatus.NEW.value,
                    ),
                )
            )
            .order_by(Task.deadline.asc(), Task.id.asc())
            .limit(limit)
        )
        result = await session.execute(stmt)
        return result.scalars().all()

    ACTIVE_STATUSES = (
        TaskStatus.NEW.value,
        TaskStatus.IN_PROGRESS.value,
        TaskStatus.AWAITING_APPROVAL.value,
    )

    @staticmethod
    async def list_active_assigned_to(
        session: AsyncSession,
        *,
        assignee_id: int,
        limit: int = 50,
    ) -> Sequence[Task]:
        """Активные задачи конкретного исполнителя для админ-обзора.

        Узкая версия list_active_for_user: только assignee-ветка, без
        неприятых-самим-собой. Грузим creator/department/files одним
        запросом — нужны для рендера карточки.
        """
        stmt = (
            select(Task)
            .options(
                selectinload(Task.creator),
                selectinload(Task.department),
                selectinload(Task.files),
            )
            .where(
                Task.assignee_id == assignee_id,
                Task.status.in_(TasksRepository.ACTIVE_STATUSES),
            )
            .order_by(Task.deadline.asc(), Task.id.asc())
            .limit(limit)
        )
        result = await session.execute(stmt)
        return result.scalars().all()

    @staticmethod
    async def workload_summary(
        session: AsyncSession,
    ) -> Sequence[tuple[int, int]]:
        """Сколько активных задач у каждого assignee.

        Возвращает [(assignee_id, count), …] для assignee'ов, у которых
        хотя бы одна активная задача. Сортировка — count DESC, чтобы
        самые загруженные сверху. assignee_id is NULL не входит.
        """
        stmt = (
            select(Task.assignee_id, func.count(Task.id))
            .where(
                Task.assignee_id.is_not(None),
                Task.status.in_(TasksRepository.ACTIVE_STATUSES),
            )
            .group_by(Task.assignee_id)
            .order_by(func.count(Task.id).desc(), Task.assignee_id.asc())
        )
        result = await session.execute(stmt)
        return [(int(aid), int(cnt)) for aid, cnt in result.all()]

    @staticmethod
    async def list_overdue(
        session: AsyncSession,
        *,
        department_id: int | None = None,
        now: datetime | None = None,
    ) -> Sequence[Task]:
        """
        Текущие просрочки: status IN (new, in_progress) AND deadline < now.
        Сортировка: deadline ASC (самые «горящие» первыми) + id ASC.
        Используется AI-инструментом get_overdue_tasks.
        """
        now_dt = now or datetime.now(timezone.utc)
        stmt = (
            select(Task)
            .where(
                Task.status.in_([TaskStatus.NEW.value, TaskStatus.IN_PROGRESS.value]),
                Task.deadline < now_dt,
            )
            .order_by(Task.deadline.asc(), Task.id.asc())
        )
        if department_id is not None:
            stmt = stmt.where(Task.department_id == department_id)
        result = await session.execute(stmt)
        return result.scalars().all()

    @staticmethod
    async def search_by_text(
        session: AsyncSession,
        *,
        query: str,
        status: str | None = None,
        department_id: int | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        limit: int = 10,
    ) -> Sequence[Task]:
        """
        Substring-поиск по title + description (case-insensitive, ILIKE).
        Без векторов — на MVP достаточно. Возвращает до `limit` задач,
        отсортированных по created_at DESC.

        Пустой/коротенький query (<2 симв.) → пустой результат.
        """
        q = (query or "").strip()
        if len(q) < 2:
            return []
        pattern = f"%{q}%"
        conds: list[Any] = [(Task.title.ilike(pattern)) | (Task.description.ilike(pattern))]
        if status is not None:
            conds.append(Task.status == status)
        if department_id is not None:
            conds.append(Task.department_id == department_id)
        if date_from is not None:
            conds.append(Task.created_at >= date_from)
        if date_to is not None:
            conds.append(Task.created_at < date_to)
        stmt = (
            select(Task).where(*conds).order_by(Task.created_at.desc(), Task.id.desc()).limit(limit)
        )
        result = await session.execute(stmt)
        return result.scalars().all()

    @staticmethod
    async def list_assignees_with_archive(
        session: AsyncSession,
        *,
        department_id: int | None = None,
    ) -> Sequence[int]:
        """
        ID юзеров, у кого есть хоть одна архивная задача. Для фильтра
        «по исполнителю» в архиве.
        """
        conds: list[Any] = [
            Task.status.in_(ARCHIVE_STATUSES),
            Task.assignee_id.isnot(None),
        ]
        if department_id is not None:
            conds.append(Task.department_id == department_id)
        result = await session.execute(select(Task.assignee_id).where(*conds).distinct())
        return [row[0] for row in result.all()]
