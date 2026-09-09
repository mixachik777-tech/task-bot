"""
Тесты AI-инструментов на сидинговой БД.

Не используют Gemini — это прямой вызов функций из app/ai/tools.py.
Покрывают permission-логику и базовое корректное содержимое ответа.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import update

from app.ai.context import UserContext
from app.ai.tools import (
    find_tasks,
    find_users,
    get_overdue_tasks,
    get_task_details,
    get_team_summary,
    get_user_history,
    get_user_tasks,
    get_user_workload,
)
from app.db.enums import HistoryEventType, TaskPriority, TaskStatus, UserRole
from app.db.models import Task
from app.db.repositories.departments import DepartmentsRepository
from app.db.repositories.history import HistoryRepository
from app.db.repositories.tasks import TasksRepository
from app.db.repositories.users import UsersRepository


def _utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


async def _seed(session):
    d = await DepartmentsRepository.create(
        session, code="ait_d", name="AIT Designers", topic_id=0
    )
    admin = await UsersRepository.create(
        session, tg_user_id=88001, full_name="Lead User", is_active=True,
        role=UserRole.ADMIN, department_id=d.id,
    )
    employee = await UsersRepository.create(
        session, tg_user_id=88002, full_name="Worker Ivanov", tg_username="ivan",
        is_active=True, role=UserRole.EMPLOYEE, department_id=d.id,
    )
    return d, admin, employee


async def _mk(session, *, dept_id, creator_id, assignee_id=None, **overrides) -> Task:
    base = {
        "title": overrides.pop("title", "T"),
        "description": overrides.pop("description", None),
        "priority": TaskPriority.MEDIUM,
        "deadline": overrides.pop("deadline", _utc(2026, 6, 1)),
        "creator_id": creator_id,
        "department_id": dept_id,
    }
    t = await TasksRepository.create(session, **base)
    values = {}
    if assignee_id is not None:
        values["assignee_id"] = assignee_id
    values.update(overrides)
    if values:
        await session.execute(update(Task).where(Task.id == t.id).values(**values))
        await session.flush()
    return await TasksRepository.get_by_id(session, t.id)


def _ctx(user, *, now=None) -> UserContext:
    return UserContext.from_user(user, now=now or _utc(2026, 5, 17, 12, 0))


# ─── get_user_tasks ───────────────────────────────────────────────────


async def test_get_user_tasks_default_to_self(session):
    d, admin, emp = await _seed(session)
    await _mk(session, dept_id=d.id, creator_id=admin.id, assignee_id=emp.id)
    result = await get_user_tasks(session, _ctx(emp))
    assert result["user_id"] == emp.id
    assert result["count"] == 1


async def test_get_user_tasks_employee_cannot_see_others(session):
    d, admin, emp = await _seed(session)
    other = await UsersRepository.create(
        session, tg_user_id=88003, full_name="Other", is_active=True,
        role=UserRole.EMPLOYEE,
    )
    await _mk(session, dept_id=d.id, creator_id=admin.id, assignee_id=other.id)
    # employee запрашивает other → должно быть подменено на self
    result = await get_user_tasks(session, _ctx(emp), user_id=other.id)
    assert result["user_id"] == emp.id
    assert result["count"] == 0


# ─── get_user_workload ────────────────────────────────────────────────


async def test_get_user_workload_returns_fields(session):
    d, admin, emp = await _seed(session)
    now = _utc(2026, 5, 17, 12, 0)
    await _mk(
        session, dept_id=d.id, creator_id=admin.id, assignee_id=emp.id,
        status=TaskStatus.IN_PROGRESS.value, accepted_at=now - timedelta(days=1),
    )
    result = await get_user_workload(session, _ctx(emp, now=now))
    assert result["user_id"] == emp.id
    assert "assigned" in result and "completed" in result
    assert result["in_progress_now"] == 1


# ─── get_team_summary ─────────────────────────────────────────────────


async def test_get_team_summary_blocked_for_employee(session):
    _, _, emp = await _seed(session)
    result = await get_team_summary(session, _ctx(emp))
    assert "error" in result


async def test_get_team_summary_admin_returns_totals_and_users(session):
    d, admin, emp = await _seed(session)
    await _mk(session, dept_id=d.id, creator_id=admin.id, assignee_id=emp.id)
    result = await get_team_summary(session, _ctx(admin))
    assert "totals" in result
    assert "users" in result
    assert any(u["user_id"] == emp.id for u in result["users"])


# ─── get_overdue_tasks ────────────────────────────────────────────────


async def test_get_overdue_tasks_employee_sees_only_self(session):
    d, admin, emp = await _seed(session)
    now = _utc(2026, 5, 17, 12, 0)
    my_overdue = await _mk(
        session, dept_id=d.id, creator_id=admin.id, assignee_id=emp.id,
        status=TaskStatus.IN_PROGRESS.value,
        deadline=now - timedelta(hours=2),
    )
    not_mine = await _mk(
        session, dept_id=d.id, creator_id=admin.id, assignee_id=admin.id,
        status=TaskStatus.IN_PROGRESS.value,
        deadline=now - timedelta(hours=2),
    )
    result = await get_overdue_tasks(session, _ctx(emp, now=now))
    ids = {t["id"] for t in result["tasks"]}
    assert my_overdue.id in ids
    assert not_mine.id not in ids


# ─── find_tasks ───────────────────────────────────────────────────────


async def test_find_tasks_substring(session):
    d, admin, _ = await _seed(session)
    await _mk(session, dept_id=d.id, creator_id=admin.id, title="Афиша 9 мая")
    await _mk(session, dept_id=d.id, creator_id=admin.id, title="Корректура")
    result = await find_tasks(session, _ctx(admin), query="афиш")
    assert result["count"] == 1


async def test_find_tasks_short_query_returns_empty(session):
    _, admin, _ = await _seed(session)
    result = await find_tasks(session, _ctx(admin), query="a")
    assert result["count"] == 0


# ─── get_task_details ─────────────────────────────────────────────────


async def test_get_task_details_full_payload(session):
    d, admin, emp = await _seed(session)
    t = await _mk(session, dept_id=d.id, creator_id=admin.id, assignee_id=emp.id, title="X")
    await HistoryRepository.log(
        session, task_id=t.id, user_id=admin.id, event_type=HistoryEventType.CREATED
    )
    result = await get_task_details(session, _ctx(admin), task_id=t.id)
    assert result["id"] == t.id
    assert result["title"] == "X"
    assert "history" in result and len(result["history"]) == 1


async def test_get_task_details_employee_denied_if_not_assignee(session):
    d, admin, emp = await _seed(session)
    t = await _mk(session, dept_id=d.id, creator_id=admin.id, assignee_id=admin.id)
    result = await get_task_details(session, _ctx(emp), task_id=t.id)
    assert "error" in result


async def test_get_task_details_not_found(session):
    _, admin, _ = await _seed(session)
    result = await get_task_details(session, _ctx(admin), task_id=99999)
    assert "error" in result


# ─── get_user_history ────────────────────────────────────────────────


async def test_get_user_history_filters_period(session):
    """
    Используем реальный now() для ctx, чтобы окно последних 7 дней
    покрыло только что записанное событие (created_at = серверный NOW).
    """
    d, admin, _ = await _seed(session)
    t = await _mk(session, dept_id=d.id, creator_id=admin.id)
    await HistoryRepository.log(
        session, task_id=t.id, user_id=admin.id, event_type=HistoryEventType.CREATED
    )
    real_now = datetime.now(timezone.utc)
    result = await get_user_history(session, _ctx(admin, now=real_now), period="week")
    assert result["count"] >= 1


# ─── find_users ──────────────────────────────────────────────────────


async def test_find_users_by_name(session):
    d, admin, emp = await _seed(session)
    result = await find_users(session, _ctx(admin), query="Ivanov")
    assert any(u["user_id"] == emp.id for u in result["users"])


async def test_find_users_by_username(session):
    d, admin, emp = await _seed(session)
    result = await find_users(session, _ctx(admin), query="ivan")
    assert any(u["user_id"] == emp.id for u in result["users"])
