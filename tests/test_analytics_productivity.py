"""
Тесты AnalyticsRepository.get_user_productivity + daily_breakdown
+ get_user_active_tasks + get_user_stale_tasks.
"""

from datetime import date, datetime, timedelta, timezone

from sqlalchemy import update

from app.db.enums import HistoryEventType, TaskPriority, TaskStatus
from app.db.models import Task, TaskHistory
from app.db.repositories.analytics import AnalyticsRepository
from app.db.repositories.departments import DepartmentsRepository
from app.db.repositories.tasks import TasksRepository
from app.db.repositories.users import UsersRepository


def _utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


async def _seed(session):
    dept = await DepartmentsRepository.create(
        session, code="prod_dept", name="Prod Dept", topic_id=0
    )
    creator = await UsersRepository.create(
        session, tg_user_id=40001, full_name="Creator", is_active=True,
    )
    ivanov = await UsersRepository.create(
        session, tg_user_id=40002, full_name="Ivan", is_active=True,
        department_id=dept.id,
    )
    return dept, creator, ivanov


async def _mk(
    session,
    *,
    dept_id: int,
    creator_id: int,
    assignee_id: int | None,
    status: str,
    created_at: datetime,
    accepted_at: datetime | None = None,
    completed_at: datetime | None = None,
    deadline: datetime | None = None,
) -> Task:
    deadline = deadline or (created_at + timedelta(days=1))
    t = await TasksRepository.create(
        session,
        title=f"T {status}",
        description=None,
        priority=TaskPriority.LOW,
        deadline=deadline,
        creator_id=creator_id,
        department_id=dept_id,
    )
    values: dict = {"status": status, "created_at": created_at}
    if assignee_id is not None:
        values["assignee_id"] = assignee_id
    if accepted_at is not None:
        values["accepted_at"] = accepted_at
    if completed_at is not None:
        values["completed_at"] = completed_at
    await session.execute(update(Task).where(Task.id == t.id).values(**values))
    await session.flush()
    return await TasksRepository.get_by_id(session, t.id)


async def test_productivity_counts_assigned_and_completed(session):
    dept, creator, ivanov = await _seed(session)
    now = _utc(2026, 5, 17, 12, 0)

    # назначена и выполнена в окне
    await _mk(
        session, dept_id=dept.id, creator_id=creator.id, assignee_id=ivanov.id,
        status=TaskStatus.DONE.value,
        created_at=now - timedelta(days=2),
        accepted_at=now - timedelta(days=2),
        completed_at=now - timedelta(days=1),
    )
    # принята в окне, но ещё в работе
    await _mk(
        session, dept_id=dept.id, creator_id=creator.id, assignee_id=ivanov.id,
        status=TaskStatus.IN_PROGRESS.value,
        created_at=now - timedelta(days=1),
        accepted_at=now - timedelta(days=1),
    )
    # не его — не должна попасть
    other = await UsersRepository.create(
        session, tg_user_id=40099, full_name="Other", is_active=True,
    )
    await _mk(
        session, dept_id=dept.id, creator_id=creator.id, assignee_id=other.id,
        status=TaskStatus.DONE.value,
        created_at=now - timedelta(days=2),
        accepted_at=now - timedelta(days=2),
        completed_at=now - timedelta(days=1),
    )

    prod = await AnalyticsRepository.get_user_productivity(
        session, user_id=ivanov.id, period="week", now=now
    )
    assert prod.assigned == 2
    assert prod.completed == 1
    assert prod.in_progress_now == 1
    assert prod.overdue_now == 0
    assert prod.avg_completion_seconds is not None


async def test_productivity_overdue_snapshot(session):
    dept, creator, ivanov = await _seed(session)
    now = _utc(2026, 5, 17, 12, 0)

    # просроченная в работе
    await _mk(
        session, dept_id=dept.id, creator_id=creator.id, assignee_id=ivanov.id,
        status=TaskStatus.IN_PROGRESS.value,
        created_at=now - timedelta(days=2),
        accepted_at=now - timedelta(days=2),
        deadline=now - timedelta(hours=3),
    )
    # уже завершённая, не overdue
    await _mk(
        session, dept_id=dept.id, creator_id=creator.id, assignee_id=ivanov.id,
        status=TaskStatus.DONE.value,
        created_at=now - timedelta(days=2),
        accepted_at=now - timedelta(days=2),
        completed_at=now - timedelta(hours=2),
        deadline=now - timedelta(hours=5),
    )
    prod = await AnalyticsRepository.get_user_productivity(
        session, user_id=ivanov.id, period="week", now=now
    )
    assert prod.overdue_now == 1


async def test_productivity_daily_breakdown_dense_array(session):
    """Дни без событий должны попасть в breakdown как (date, 0, 0)."""
    dept, creator, ivanov = await _seed(session)
    now = _utc(2026, 5, 17, 12, 0)
    # одно событие 14 мая, ничего больше — для week (10-17 мая) должны быть нули
    await _mk(
        session, dept_id=dept.id, creator_id=creator.id, assignee_id=ivanov.id,
        status=TaskStatus.DONE.value,
        created_at=_utc(2026, 5, 14, 8, 0),
        accepted_at=_utc(2026, 5, 14, 9, 0),
        completed_at=_utc(2026, 5, 14, 14, 0),
    )
    prod = await AnalyticsRepository.get_user_productivity(
        session, user_id=ivanov.id, period="week", now=now
    )
    days = [d for d, _, _ in prod.daily_breakdown]
    # период rolling 7d — должно быть несколько дней
    assert len(days) >= 7
    # день 14 мая присутствует с assigned=1 и completed=1
    by_day = {d: (a, c) for d, a, c in prod.daily_breakdown}
    assert by_day.get(date(2026, 5, 14)) == (1, 1)
    # любой другой день — нули
    zero_days = [
        d for d, a, c in prod.daily_breakdown if d != date(2026, 5, 14)
    ]
    for d in zero_days:
        assert by_day[d] == (0, 0)


async def test_productivity_empty_user(session):
    dept, _, ivanov = await _seed(session)
    now = _utc(2026, 5, 17, 12, 0)
    prod = await AnalyticsRepository.get_user_productivity(
        session, user_id=ivanov.id, period="week", now=now
    )
    assert prod.assigned == 0
    assert prod.completed == 0
    assert prod.cancelled == 0
    assert prod.on_time_count == 0
    assert prod.completion_rate_pct is None
    assert prod.on_time_rate_pct is None
    assert prod.avg_completion_seconds is None
    # breakdown — массив нулей
    assert all(a == 0 and c == 0 for _, a, c in prod.daily_breakdown)


async def test_productivity_on_time_and_completion_rate(session):
    """% выполнения и % в срок — на смешанном наборе done/cancelled/late."""
    dept, creator, ivanov = await _seed(session)
    now = _utc(2026, 5, 17, 12, 0)

    # принял и закрыл В СРОК (completed_at <= deadline)
    await _mk(
        session, dept_id=dept.id, creator_id=creator.id, assignee_id=ivanov.id,
        status=TaskStatus.DONE.value,
        created_at=now - timedelta(days=3),
        accepted_at=now - timedelta(days=3),
        completed_at=now - timedelta(days=2),
        deadline=now - timedelta(days=1),
    )
    # принял и закрыл ПОСЛЕ дедлайна
    await _mk(
        session, dept_id=dept.id, creator_id=creator.id, assignee_id=ivanov.id,
        status=TaskStatus.DONE.value,
        created_at=now - timedelta(days=3),
        accepted_at=now - timedelta(days=3),
        completed_at=now - timedelta(days=1),
        deadline=now - timedelta(days=2),
    )
    # принял, ещё в работе — assigned=+1, completed нет
    await _mk(
        session, dept_id=dept.id, creator_id=creator.id, assignee_id=ivanov.id,
        status=TaskStatus.IN_PROGRESS.value,
        created_at=now - timedelta(days=2),
        accepted_at=now - timedelta(days=2),
    )

    prod = await AnalyticsRepository.get_user_productivity(
        session, user_id=ivanov.id, period="week", now=now
    )
    assert prod.assigned == 3
    assert prod.completed == 2
    assert prod.on_time_count == 1
    # 2/3 = 66.7%
    assert prod.completion_rate_pct == 66.7
    # 1/2 = 50.0%
    assert prod.on_time_rate_pct == 50.0


async def test_productivity_cancelled_via_history(session):
    """cancelled берётся из task_history.event=cancelled в окне."""
    dept, creator, ivanov = await _seed(session)
    now = _utc(2026, 5, 17, 12, 0)

    # отменённая задача в окне: assignee=ivanov, есть событие cancelled
    t_in = await _mk(
        session, dept_id=dept.id, creator_id=creator.id, assignee_id=ivanov.id,
        status=TaskStatus.CANCELLED.value,
        created_at=now - timedelta(days=3),
        accepted_at=now - timedelta(days=3),
    )
    h = TaskHistory(
        task_id=t_in.id, user_id=creator.id,
        event_type=HistoryEventType.CANCELLED.value, payload={"by_role": "creator"},
    )
    session.add(h)
    await session.flush()
    # принудительно простявляем created_at в окне (server_default=now() выставит
    # текущее «реальное» время, нам нужно зафиксированное для проверки)
    await session.execute(
        update(TaskHistory).where(TaskHistory.id == h.id).values(
            created_at=now - timedelta(days=2)
        )
    )
    await session.flush()

    # отменённая задача ВНЕ окна — не должна засчитаться
    t_out = await _mk(
        session, dept_id=dept.id, creator_id=creator.id, assignee_id=ivanov.id,
        status=TaskStatus.CANCELLED.value,
        created_at=now - timedelta(days=30),
        accepted_at=now - timedelta(days=30),
    )
    h2 = TaskHistory(
        task_id=t_out.id, user_id=creator.id,
        event_type=HistoryEventType.CANCELLED.value, payload={"by_role": "creator"},
    )
    session.add(h2)
    await session.flush()
    await session.execute(
        update(TaskHistory).where(TaskHistory.id == h2.id).values(
            created_at=now - timedelta(days=20)
        )
    )
    await session.flush()

    prod = await AnalyticsRepository.get_user_productivity(
        session, user_id=ivanov.id, period="week", now=now
    )
    assert prod.cancelled == 1


# ---------- get_user_active_tasks ----------


async def test_active_tasks_bucket_layering(session):
    """Каждая задача попадает в ровно одну корзину по правилу-лесенке."""
    dept, creator, ivanov = await _seed(session)
    now = _utc(2026, 5, 17, 12, 0)

    # 1) overdue
    await _mk(
        session, dept_id=dept.id, creator_id=creator.id, assignee_id=ivanov.id,
        status=TaskStatus.IN_PROGRESS.value,
        created_at=now - timedelta(days=3),
        accepted_at=now - timedelta(days=3),
        deadline=now - timedelta(hours=5),
    )
    # 2) burning_24h: дедлайн через 6 часов
    await _mk(
        session, dept_id=dept.id, creator_id=creator.id, assignee_id=ivanov.id,
        status=TaskStatus.IN_PROGRESS.value,
        created_at=now - timedelta(days=1),
        accepted_at=now - timedelta(hours=20),
        deadline=now + timedelta(hours=6),
    )
    # 3) this_week: через 3 дня
    await _mk(
        session, dept_id=dept.id, creator_id=creator.id, assignee_id=ivanov.id,
        status=TaskStatus.NEW.value,
        created_at=now - timedelta(hours=2),
        deadline=now + timedelta(days=3),
    )
    # 4) later: через 14 дней
    await _mk(
        session, dept_id=dept.id, creator_id=creator.id, assignee_id=ivanov.id,
        status=TaskStatus.IN_PROGRESS.value,
        created_at=now - timedelta(hours=2),
        accepted_at=now - timedelta(hours=1),
        deadline=now + timedelta(days=14),
    )
    # уже завершённая — НЕ должна попасть ни в одну корзину
    await _mk(
        session, dept_id=dept.id, creator_id=creator.id, assignee_id=ivanov.id,
        status=TaskStatus.DONE.value,
        created_at=now - timedelta(days=2),
        accepted_at=now - timedelta(days=2),
        completed_at=now - timedelta(hours=3),
        deadline=now + timedelta(days=5),
    )

    buckets = await AnalyticsRepository.get_user_active_tasks(
        session, user_id=ivanov.id, now=now
    )
    assert len(buckets.overdue) == 1
    assert len(buckets.burning_24h) == 1
    assert len(buckets.this_week) == 1
    assert len(buckets.later) == 1
    assert buckets.total == 4


async def test_active_tasks_isolated_per_user(session):
    """Задачи других assignee в кабинет не попадают."""
    dept, creator, ivanov = await _seed(session)
    other = await UsersRepository.create(
        session, tg_user_id=40299, full_name="Other2", is_active=True,
    )
    now = _utc(2026, 5, 17, 12, 0)

    await _mk(
        session, dept_id=dept.id, creator_id=creator.id, assignee_id=other.id,
        status=TaskStatus.IN_PROGRESS.value,
        created_at=now - timedelta(days=1),
        accepted_at=now - timedelta(days=1),
        deadline=now + timedelta(hours=6),
    )
    buckets = await AnalyticsRepository.get_user_active_tasks(
        session, user_id=ivanov.id, now=now
    )
    assert buckets.total == 0


# ---------- get_user_stale_tasks ----------


async def test_stale_tasks_by_last_history_event(session):
    """Залипшая = in_progress + последнее событие > N дней назад."""
    dept, creator, ivanov = await _seed(session)
    now = _utc(2026, 5, 17, 12, 0)

    # залипшая: accepted 10 дней назад, новых событий нет
    stale = await _mk(
        session, dept_id=dept.id, creator_id=creator.id, assignee_id=ivanov.id,
        status=TaskStatus.IN_PROGRESS.value,
        created_at=now - timedelta(days=10),
        accepted_at=now - timedelta(days=10),
        deadline=now + timedelta(days=5),
    )
    # последняя история — accepted 10 дней назад
    h = TaskHistory(
        task_id=stale.id, user_id=ivanov.id,
        event_type=HistoryEventType.ACCEPTED.value,
    )
    session.add(h)
    await session.flush()
    await session.execute(
        update(TaskHistory).where(TaskHistory.id == h.id).values(
            created_at=now - timedelta(days=10)
        )
    )

    # НЕ залипшая: accepted 10 дней назад, но был коммент 2 дня назад
    fresh = await _mk(
        session, dept_id=dept.id, creator_id=creator.id, assignee_id=ivanov.id,
        status=TaskStatus.IN_PROGRESS.value,
        created_at=now - timedelta(days=10),
        accepted_at=now - timedelta(days=10),
        deadline=now + timedelta(days=5),
    )
    h2 = TaskHistory(
        task_id=fresh.id, user_id=ivanov.id,
        event_type=HistoryEventType.COMMENTED.value, payload={"text": "ok"},
    )
    session.add(h2)
    await session.flush()
    await session.execute(
        update(TaskHistory).where(TaskHistory.id == h2.id).values(
            created_at=now - timedelta(days=2)
        )
    )
    await session.flush()

    result = await AnalyticsRepository.get_user_stale_tasks(
        session, user_id=ivanov.id, stale_days=7, now=now
    )
    ids = {t.id for t in result}
    assert stale.id in ids
    assert fresh.id not in ids
