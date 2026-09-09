"""
Реализация tool-функций AI-агента.

Все функции:
- Принимают (session, ctx: UserContext, **kwargs) и возвращают
  JSON-сериализуемый dict.
- Только READ. Не вызывают INSERT/UPDATE в основные таблицы.
- Сами проверяют permission по `ctx.role` (employee видит только своё).
- Возвращают компактные структуры — длинные поля (description, history)
  отдаются только в get_task_details, чтобы экономить контекст модели.

Tool-handler-маппинг — словарь `TOOL_HANDLERS` в конце файла; используется
агентом для диспатча после tool_call.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.context import UserContext
from app.bot.utils.time import format_dt_local
from app.db.enums import TaskStatus, UserRole
from app.db.models import User
from app.db.repositories.analytics import (
    AnalyticsRepository,
    period_bounds_utc,
)
from app.db.repositories.departments import DepartmentsRepository
from app.db.repositories.history import HistoryRepository
from app.db.repositories.tasks import TasksRepository
from app.db.repositories.users import UsersRepository

# ──────────────────────────────────────────────────────────────────────
# helpers
# ──────────────────────────────────────────────────────────────────────


def _is_employee(ctx: UserContext) -> bool:
    return ctx.role == UserRole.EMPLOYEE.value


def _format_user(u: User | None) -> str | None:
    if u is None:
        return None
    if u.tg_username:
        return f"@{u.tg_username}"
    return u.full_name or f"id={u.tg_user_id}"


def _format_task(task, *, now_utc: datetime) -> dict[str, Any]:
    is_overdue = (
        task.status in (TaskStatus.NEW.value, TaskStatus.IN_PROGRESS.value)
        and task.deadline < now_utc
    )
    return {
        "id": task.id,
        "title": task.title,
        "status": task.status,
        "priority": task.priority,
        "department_id": task.department_id,
        "deadline": format_dt_local(task.deadline),
        "is_overdue": is_overdue,
        "assignee_id": task.assignee_id,
        "creator_id": task.creator_id,
    }


def _format_period_label(period: str) -> str:
    return {"today": "сегодня", "week": "за 7 дней", "month": "за 30 дней"}.get(
        period, period
    )


async def _resolve_dept_id(
    session: AsyncSession, dept_code: str | None
) -> int | None:
    if not dept_code:
        return None
    dept = await DepartmentsRepository.get_by_code(session, dept_code)
    return dept.id if dept else None


# ──────────────────────────────────────────────────────────────────────
# tools
# ──────────────────────────────────────────────────────────────────────


async def get_user_tasks(
    session: AsyncSession,
    ctx: UserContext,
    *,
    user_id: int | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    """Задачи, где юзер creator или assignee. Employee всегда видит только себя."""
    if _is_employee(ctx):
        user_id = ctx.user_id
    target_id = user_id if user_id is not None else ctx.user_id

    if status is not None and status not in {
        s.value for s in TaskStatus
    }:
        return {"error": f"unknown status: {status}"}

    # активные через list_active (с фильтром по dept нет — поэтому ниже SQL вручную)
    from sqlalchemy import or_

    from app.db.models import Task

    conds = [or_(Task.creator_id == target_id, Task.assignee_id == target_id)]
    if status is not None:
        conds.append(Task.status == status)
    stmt = select(Task).where(*conds).order_by(Task.deadline.asc(), Task.id.asc()).limit(50)
    rows = (await session.execute(stmt)).scalars().all()
    return {
        "user_id": target_id,
        "status_filter": status,
        "count": len(rows),
        "tasks": [_format_task(t, now_utc=ctx.now_utc) for t in rows],
    }


async def get_user_workload(
    session: AsyncSession,
    ctx: UserContext,
    *,
    user_id: int | None = None,
    period: str = "week",
) -> dict[str, Any]:
    """Сводка нагрузки одного юзера. Employee → forced на себя."""
    if _is_employee(ctx):
        user_id = ctx.user_id
    target_id = user_id if user_id is not None else ctx.user_id

    if period not in {"today", "week", "month"}:
        return {"error": f"unknown period: {period}"}

    prod = await AnalyticsRepository.get_user_productivity(
        session, user_id=target_id, period=period, now=ctx.now_utc
    )
    target = await UsersRepository.get_by_id(session, target_id)
    return {
        "user_id": target_id,
        "user_label": _format_user(target),
        "period": period,
        "period_label": _format_period_label(period),
        "assigned": prod.assigned,
        "completed": prod.completed,
        "in_progress_now": prod.in_progress_now,
        "overdue_now": prod.overdue_now,
        "avg_completion_seconds": prod.avg_completion_seconds,
    }


async def get_team_summary(
    session: AsyncSession,
    ctx: UserContext,
    *,
    period: str = "week",
) -> dict[str, Any]:
    """Командная сводка: общее + поюзерно. Только lead/admin."""
    if _is_employee(ctx):
        return {"error": "Только для руководителей и админов."}
    if period not in {"today", "week", "month"}:
        return {"error": f"unknown period: {period}"}

    summary = await AnalyticsRepository.get_summary(
        session, scope="all", period=period, now=ctx.now_utc
    )
    users = list(await UsersRepository.list_active_with_department(session))
    per_user: list[dict[str, Any]] = []
    for u in users:
        prod = await AnalyticsRepository.get_user_productivity(
            session, user_id=u.id, period=period, now=ctx.now_utc
        )
        per_user.append(
            {
                "user_id": u.id,
                "user_label": _format_user(u),
                "department_id": u.department_id,
                "assigned": prod.assigned,
                "completed": prod.completed,
                "in_progress_now": prod.in_progress_now,
                "overdue_now": prod.overdue_now,
            }
        )
    return {
        "period": period,
        "period_label": _format_period_label(period),
        "totals": {
            "created": summary.created,
            "completed": summary.completed,
            "in_progress": summary.in_progress,
            "overdue": summary.overdue,
            "avg_completion_seconds": summary.avg_completion_seconds,
        },
        "users": per_user,
    }


async def get_overdue_tasks(
    session: AsyncSession,
    ctx: UserContext,
    *,
    dept_code: str | None = None,
) -> dict[str, Any]:
    """Текущие просрочки. Employee видит только те, где он assignee."""
    dept_id = await _resolve_dept_id(session, dept_code)
    rows = await TasksRepository.list_overdue(
        session, department_id=dept_id, now=ctx.now_utc
    )
    if _is_employee(ctx):
        rows = [t for t in rows if t.assignee_id == ctx.user_id]
    return {
        "dept_code": dept_code,
        "count": len(rows),
        "tasks": [_format_task(t, now_utc=ctx.now_utc) for t in rows[:30]],
    }


async def find_tasks(
    session: AsyncSession,
    ctx: UserContext,
    *,
    query: str,
    status: str | None = None,
    dept_code: str | None = None,
    period: str | None = None,
) -> dict[str, Any]:
    """Substring-поиск по title+description. Employee — только свои."""
    date_from: datetime | None = None
    date_to: datetime | None = None
    if period:
        if period not in {"today", "week", "month"}:
            return {"error": f"unknown period: {period}"}
        date_from, date_to = period_bounds_utc(period, now=ctx.now_utc)
    if status is not None and status not in {s.value for s in TaskStatus}:
        return {"error": f"unknown status: {status}"}

    dept_id = await _resolve_dept_id(session, dept_code)
    rows = await TasksRepository.search_by_text(
        session,
        query=query,
        status=status,
        department_id=dept_id,
        date_from=date_from,
        date_to=date_to,
        limit=20,
    )
    if _is_employee(ctx):
        rows = [
            t for t in rows
            if t.assignee_id == ctx.user_id or t.creator_id == ctx.user_id
        ]
    return {
        "query": query,
        "count": len(rows),
        "tasks": [_format_task(t, now_utc=ctx.now_utc) for t in rows],
    }


async def get_task_details(
    session: AsyncSession,
    ctx: UserContext,
    *,
    task_id: int,
) -> dict[str, Any]:
    """Полная карточка + история. Employee — только если он assignee."""
    task = await TasksRepository.get_full(session, task_id)
    if task is None:
        return {"error": "task не найдена"}
    if _is_employee(ctx) and task.assignee_id != ctx.user_id:
        return {"error": "доступ к этой задаче ограничен"}
    history = await HistoryRepository.list_by_task(session, task_id)
    actor_ids = {h.user_id for h in history if h.user_id is not None}
    actors = (
        await UsersRepository.list_by_ids(session, actor_ids) if actor_ids else []
    )
    actor_by_id = {u.id: u for u in actors}
    return {
        "id": task.id,
        "title": task.title,
        "description": task.description,
        "status": task.status,
        "priority": task.priority,
        "department_id": task.department_id,
        "department_name": task.department.name if task.department else None,
        "deadline": format_dt_local(task.deadline),
        "is_overdue": (
            task.status in (TaskStatus.NEW.value, TaskStatus.IN_PROGRESS.value)
            and task.deadline < ctx.now_utc
        ),
        "creator_label": _format_user(task.creator),
        "assignee_label": _format_user(task.assignee),
        "files_count": len(task.files),
        "history": [
            {
                "at": format_dt_local(h.created_at),
                "event": h.event_type,
                "actor": _format_user(actor_by_id.get(h.user_id)) if h.user_id else None,
                "payload": h.payload or {},
            }
            for h in history
        ],
    }


async def get_user_history(
    session: AsyncSession,
    ctx: UserContext,
    *,
    user_id: int | None = None,
    period: str = "week",
) -> dict[str, Any]:
    """События задач, инициированные юзером в окне. Employee → forced self."""
    if _is_employee(ctx):
        user_id = ctx.user_id
    target_id = user_id if user_id is not None else ctx.user_id
    if period not in {"today", "week", "month"}:
        return {"error": f"unknown period: {period}"}
    f, t = period_bounds_utc(period, now=ctx.now_utc)
    events = await HistoryRepository.list_by_user_period(
        session, user_id=target_id, date_from=f, date_to=t
    )
    return {
        "user_id": target_id,
        "period": period,
        "period_label": _format_period_label(period),
        "count": len(events),
        "events": [
            {
                "at": format_dt_local(e.created_at),
                "event": e.event_type,
                "task_id": e.task_id,
                "payload": e.payload or {},
            }
            for e in events
        ],
    }


async def find_users(
    session: AsyncSession,
    ctx: UserContext,
    *,
    query: str,
    dept_code: str | None = None,
) -> dict[str, Any]:
    """Substring-поиск по full_name и tg_username. Доступно всем активным."""
    q = (query or "").strip()
    if len(q) < 2:
        return {"query": query, "count": 0, "users": []}
    dept_id = await _resolve_dept_id(session, dept_code)

    from app.db.models import User as UserModel

    pattern = f"%{q}%"
    conds = [
        UserModel.is_active.is_(True),
        (UserModel.full_name.ilike(pattern)) | (UserModel.tg_username.ilike(pattern)),
    ]
    if dept_id is not None:
        conds.append(UserModel.department_id == dept_id)
    rows = (
        await session.execute(
            select(UserModel).where(*conds).order_by(UserModel.full_name).limit(20)
        )
    ).scalars().all()
    return {
        "query": query,
        "count": len(rows),
        "users": [
            {
                "user_id": u.id,
                "label": _format_user(u),
                "role": u.role,
                "department_id": u.department_id,
            }
            for u in rows
        ],
    }


# ──────────────────────────────────────────────────────────────────────
# tool dispatcher: имя → handler
# ──────────────────────────────────────────────────────────────────────

ToolHandler = Callable[..., Awaitable[dict[str, Any]]]

TOOL_HANDLERS: dict[str, ToolHandler] = {
    "get_user_tasks": get_user_tasks,
    "get_user_workload": get_user_workload,
    "get_team_summary": get_team_summary,
    "get_overdue_tasks": get_overdue_tasks,
    "find_tasks": find_tasks,
    "get_task_details": get_task_details,
    "get_user_history": get_user_history,
    "find_users": find_users,
}
