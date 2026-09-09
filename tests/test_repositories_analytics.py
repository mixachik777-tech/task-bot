"""
Тесты AnalyticsRepository.get_summary + helper period_bounds_utc.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import update

from app.db.enums import TaskPriority, TaskStatus
from app.db.models import Task
from app.db.repositories.analytics import AnalyticsRepository, period_bounds_utc
from app.db.repositories.departments import DepartmentsRepository
from app.db.repositories.tasks import TasksRepository
from app.db.repositories.users import UsersRepository


def _utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


# --- period_bounds_utc unit ---


def test_period_bounds_week_is_rolling():
    now = _utc(2026, 5, 17, 12, 0)
    f, t = period_bounds_utc("week", now=now)
    assert t == now
    assert f == now - timedelta(days=7)


def test_period_bounds_month_is_rolling_30d():
    now = _utc(2026, 5, 17, 12, 0)
    f, t = period_bounds_utc("month", now=now)
    assert f == now - timedelta(days=30)


def test_period_bounds_today_is_local_midnight():
    # MSK = UTC+3. 17 мая 02:00 UTC = 17 мая 05:00 MSK → начало суток MSK = 16 мая 21:00 UTC
    now = _utc(2026, 5, 17, 2, 0)
    f, _ = period_bounds_utc("today", now=now)
    assert f == _utc(2026, 5, 16, 21, 0)


def test_period_bounds_unknown_raises():
    try:
        period_bounds_utc("year")
    except ValueError:
        return
    assert False, "expected ValueError"


# --- get_summary integration ---


async def _seed(session):
    dept = await DepartmentsRepository.create(
        session, code="an_dept", name="Analytics Dept", topic_id=0
    )
    creator = await UsersRepository.create(
        session, tg_user_id=30001, full_name="Creator", is_active=True,
        department_id=dept.id,
    )
    ivanov = await UsersRepository.create(
        session, tg_user_id=30002, full_name="Ivan", is_active=True,
        department_id=dept.id,
    )
    return dept, creator, ivanov


async def _make_task(
    session,
    *,
    dept_id: int,
    creator_id: int,
    assignee_id: int | None,
    status: str,
    created_at: datetime,
    completed_at: datetime | None = None,
    deadline: datetime | None = None,
) -> Task:
    deadline = deadline or (created_at + timedelta(days=1))
    task = await TasksRepository.create(
        session,
        title=f"T {status}",
        description=None,
        priority=TaskPriority.LOW,
        deadline=deadline,
        creator_id=creator_id,
        department_id=dept_id,
    )
    values = {"status": status, "created_at": created_at}
    if assignee_id is not None:
        values["assignee_id"] = assignee_id
    if completed_at is not None:
        values["completed_at"] = completed_at
    await session.execute(update(Task).where(Task.id == task.id).values(**values))
    await session.flush()
    return await TasksRepository.get_by_id(session, task.id)


async def test_get_summary_scope_all_week(session):
    dept, creator, ivanov = await _seed(session)
    now = _utc(2026, 5, 17, 12, 0)

    # created внутри окна (now-7d, now)
    await _make_task(
        session, dept_id=dept.id, creator_id=creator.id, assignee_id=ivanov.id,
        status=TaskStatus.DONE.value,
        created_at=now - timedelta(days=2),
        completed_at=now - timedelta(days=1),
    )
    await _make_task(
        session, dept_id=dept.id, creator_id=creator.id, assignee_id=ivanov.id,
        status=TaskStatus.IN_PROGRESS.value,
        created_at=now - timedelta(days=1),
    )
    # вне окна — не должна засчитаться в created/completed
    await _make_task(
        session, dept_id=dept.id, creator_id=creator.id, assignee_id=None,
        status=TaskStatus.DONE.value,
        created_at=now - timedelta(days=30),
        completed_at=now - timedelta(days=29),
    )

    s = await AnalyticsRepository.get_summary(
        session, scope="all", period="week", now=now
    )
    assert s.created == 2
    assert s.completed == 1
    assert s.in_progress == 1
    assert s.overdue == 0
    assert s.avg_completion_seconds is not None
    assert 60 * 60 < s.avg_completion_seconds < 86400 * 2


async def test_get_summary_overdue_current_snapshot(session):
    dept, creator, ivanov = await _seed(session)
    now = _utc(2026, 5, 17, 12, 0)

    await _make_task(
        session, dept_id=dept.id, creator_id=creator.id, assignee_id=ivanov.id,
        status=TaskStatus.IN_PROGRESS.value,
        created_at=now - timedelta(days=2),
        deadline=now - timedelta(hours=3),
    )
    await _make_task(
        session, dept_id=dept.id, creator_id=creator.id, assignee_id=None,
        status=TaskStatus.NEW.value,
        created_at=now - timedelta(hours=20),
        deadline=now - timedelta(hours=1),
    )
    # уже завершённая — НЕ overdue
    await _make_task(
        session, dept_id=dept.id, creator_id=creator.id, assignee_id=ivanov.id,
        status=TaskStatus.DONE.value,
        created_at=now - timedelta(days=2),
        completed_at=now - timedelta(hours=2),
        deadline=now - timedelta(hours=4),
    )

    s = await AnalyticsRepository.get_summary(
        session, scope="all", period="week", now=now
    )
    assert s.overdue == 2


async def test_get_summary_scope_user_isolates_other_users(session):
    dept, creator, ivanov = await _seed(session)
    now = _utc(2026, 5, 17, 12, 0)

    # задача, где ivanov assignee
    await _make_task(
        session, dept_id=dept.id, creator_id=creator.id, assignee_id=ivanov.id,
        status=TaskStatus.DONE.value,
        created_at=now - timedelta(days=2),
        completed_at=now - timedelta(days=1),
    )
    # задача, где ivanov ни creator, ни assignee — НЕ должна попасть в scope=user(ivanov)
    other_user = await UsersRepository.create(
        session, tg_user_id=30099, full_name="Other", is_active=True,
    )
    await _make_task(
        session, dept_id=dept.id, creator_id=other_user.id, assignee_id=None,
        status=TaskStatus.DONE.value,
        created_at=now - timedelta(days=2),
        completed_at=now - timedelta(days=1),
    )

    s = await AnalyticsRepository.get_summary(
        session, scope="user", user_id=ivanov.id, period="week", now=now
    )
    assert s.created == 1
    assert s.completed == 1


async def test_get_summary_scope_department_filters(session):
    dept, creator, ivanov = await _seed(session)
    now = _utc(2026, 5, 17, 12, 0)

    other_dept = await DepartmentsRepository.create(
        session, code="an_other", name="Other", topic_id=0
    )
    # своя
    await _make_task(
        session, dept_id=dept.id, creator_id=creator.id, assignee_id=ivanov.id,
        status=TaskStatus.DONE.value,
        created_at=now - timedelta(days=1),
        completed_at=now - timedelta(hours=12),
    )
    # чужой отдел
    await _make_task(
        session, dept_id=other_dept.id, creator_id=creator.id, assignee_id=None,
        status=TaskStatus.DONE.value,
        created_at=now - timedelta(days=1),
        completed_at=now - timedelta(hours=12),
    )
    s = await AnalyticsRepository.get_summary(
        session, scope="department", department_id=dept.id, period="week", now=now
    )
    assert s.completed == 1


async def test_get_summary_empty_avg_is_none(session):
    dept, creator, _ = await _seed(session)
    now = _utc(2026, 5, 17, 12, 0)
    s = await AnalyticsRepository.get_summary(
        session, scope="all", period="week", now=now
    )
    assert s.created == 0
    assert s.completed == 0
    assert s.avg_completion_seconds is None
