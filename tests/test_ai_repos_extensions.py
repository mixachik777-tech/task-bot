"""
Тесты новых репо-методов под AI:
- TasksRepository.list_overdue
- TasksRepository.search_by_text
- HistoryRepository.list_by_user_period
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import update

from app.db.enums import HistoryEventType, TaskPriority, TaskStatus
from app.db.models import Task
from app.db.repositories.departments import DepartmentsRepository
from app.db.repositories.history import HistoryRepository
from app.db.repositories.tasks import TasksRepository
from app.db.repositories.users import UsersRepository


def _utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


async def _seed(session):
    d = await DepartmentsRepository.create(session, code="ai_d", name="AI Dept", topic_id=0)
    u = await UsersRepository.create(
        session, tg_user_id=99001, full_name="AI Tester", is_active=True
    )
    return d, u


async def _mk_task(session, *, dept_id, creator_id, **overrides) -> Task:
    base = {
        "title": overrides.pop("title", "test"),
        "description": overrides.pop("description", None),
        "priority": TaskPriority.LOW,
        "deadline": overrides.pop("deadline", _utc(2026, 6, 1)),
        "creator_id": creator_id,
        "department_id": dept_id,
    }
    t = await TasksRepository.create(session, **base)
    if overrides:
        await session.execute(update(Task).where(Task.id == t.id).values(**overrides))
        await session.flush()
    return await TasksRepository.get_by_id(session, t.id)


# ─── list_overdue ─────────────────────────────────────────────────────


async def test_list_overdue_only_active_with_past_deadline(session):
    d, u = await _seed(session)
    now = _utc(2026, 5, 17, 12, 0)
    overdue = await _mk_task(
        session, dept_id=d.id, creator_id=u.id,
        status=TaskStatus.IN_PROGRESS.value,
        deadline=now - timedelta(hours=3),
    )
    not_overdue_future = await _mk_task(
        session, dept_id=d.id, creator_id=u.id,
        status=TaskStatus.NEW.value,
        deadline=now + timedelta(hours=1),
    )
    done_past = await _mk_task(
        session, dept_id=d.id, creator_id=u.id,
        status=TaskStatus.DONE.value,
        deadline=now - timedelta(hours=5),
    )
    rows = await TasksRepository.list_overdue(session, now=now)
    ids = {t.id for t in rows}
    assert overdue.id in ids
    assert not_overdue_future.id not in ids
    assert done_past.id not in ids


async def test_list_overdue_filter_by_department(session):
    d, u = await _seed(session)
    other = await DepartmentsRepository.create(
        session, code="ai_other", name="AI Other", topic_id=0
    )
    now = _utc(2026, 5, 17, 12, 0)
    in_d = await _mk_task(
        session, dept_id=d.id, creator_id=u.id,
        status=TaskStatus.IN_PROGRESS.value,
        deadline=now - timedelta(hours=1),
    )
    in_other = await _mk_task(
        session, dept_id=other.id, creator_id=u.id,
        status=TaskStatus.IN_PROGRESS.value,
        deadline=now - timedelta(hours=1),
    )
    rows = await TasksRepository.list_overdue(session, department_id=d.id, now=now)
    ids = {t.id for t in rows}
    assert in_d.id in ids
    assert in_other.id not in ids


# ─── search_by_text ───────────────────────────────────────────────────


async def test_search_by_text_finds_substring_in_title(session):
    d, u = await _seed(session)
    await _mk_task(session, dept_id=d.id, creator_id=u.id, title="Афиша 9 мая")
    await _mk_task(session, dept_id=d.id, creator_id=u.id, title="Корректура текста")
    rows = await TasksRepository.search_by_text(session, query="афиш")
    assert len(rows) == 1
    assert "Афиша" in rows[0].title


async def test_search_by_text_searches_description_too(session):
    d, u = await _seed(session)
    await _mk_task(
        session, dept_id=d.id, creator_id=u.id,
        title="Без слов", description="Нужно сверстать обложку",
    )
    rows = await TasksRepository.search_by_text(session, query="обложк")
    assert len(rows) == 1


async def test_search_by_text_short_query_returns_empty(session):
    rows = await TasksRepository.search_by_text(session, query="a")
    assert rows == []


# ─── history.list_by_user_period ──────────────────────────────────────


async def test_history_by_user_period(session):
    d, u = await _seed(session)
    t = await _mk_task(session, dept_id=d.id, creator_id=u.id)
    # один event в окне
    await HistoryRepository.log(
        session,
        task_id=t.id, user_id=u.id,
        event_type=HistoryEventType.CREATED,
    )
    from sqlalchemy import update as upd
    from app.db.models import TaskHistory
    # вне окна — задвинем created_at в прошлое
    old = await HistoryRepository.log(
        session,
        task_id=t.id, user_id=u.id, event_type=HistoryEventType.ACCEPTED,
    )
    await session.execute(
        upd(TaskHistory).where(TaskHistory.id == old.id).values(
            created_at=_utc(2025, 1, 1)
        )
    )
    await session.flush()
    events = await HistoryRepository.list_by_user_period(
        session,
        user_id=u.id,
        date_from=_utc(2026, 1, 1),
        date_to=_utc(2027, 1, 1),
    )
    assert len(events) == 1
    assert events[0].event_type == HistoryEventType.CREATED.value
