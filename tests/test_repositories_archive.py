"""
Тесты архивных методов TasksRepository: list_archive, count_archive,
list_assignees_with_archive.
"""

from datetime import datetime, timezone

from sqlalchemy import update

from app.db.enums import TaskPriority, TaskStatus
from app.db.models import Task
from app.db.repositories.departments import DepartmentsRepository
from app.db.repositories.tasks import TasksRepository
from app.db.repositories.users import UsersRepository


def _utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


async def _seed(session):
    dept_a = await DepartmentsRepository.create(
        session, code="arc_a", name="Dept A", topic_id=0
    )
    dept_b = await DepartmentsRepository.create(
        session, code="arc_b", name="Dept B", topic_id=0
    )
    creator = await UsersRepository.create(
        session, tg_user_id=20001, full_name="Creator", is_active=True
    )
    ivanov = await UsersRepository.create(
        session, tg_user_id=20002, full_name="Ivanov", is_active=True,
        department_id=dept_a.id,
    )
    petrov = await UsersRepository.create(
        session, tg_user_id=20003, full_name="Petrov", is_active=True,
        department_id=dept_b.id,
    )
    return dept_a, dept_b, creator, ivanov, petrov


async def _make_task(
    session,
    *,
    dept_id: int,
    creator_id: int,
    assignee_id: int | None = None,
    status: str,
    completed_at: datetime | None = None,
    deadline: datetime | None = None,
) -> Task:
    deadline = deadline or _utc(2026, 6, 1, 12, 0)
    task = await TasksRepository.create(
        session,
        title=f"Task {status}",
        description=None,
        priority=TaskPriority.LOW,
        deadline=deadline,
        creator_id=creator_id,
        department_id=dept_id,
    )
    values = {"status": status}
    if assignee_id is not None:
        values["assignee_id"] = assignee_id
    if completed_at is not None:
        values["completed_at"] = completed_at
    await session.execute(update(Task).where(Task.id == task.id).values(**values))
    await session.flush()
    return await TasksRepository.get_by_id(session, task.id)


async def test_list_archive_returns_only_archived(session):
    dept_a, _, creator, ivanov, _ = await _seed(session)
    # активные не должны попасть
    await _make_task(
        session, dept_id=dept_a.id, creator_id=creator.id, status=TaskStatus.NEW.value
    )
    await _make_task(
        session, dept_id=dept_a.id, creator_id=creator.id,
        assignee_id=ivanov.id, status=TaskStatus.IN_PROGRESS.value,
    )
    done = await _make_task(
        session, dept_id=dept_a.id, creator_id=creator.id,
        assignee_id=ivanov.id, status=TaskStatus.DONE.value,
        completed_at=_utc(2026, 5, 10, 12, 0),
    )
    cancelled = await _make_task(
        session, dept_id=dept_a.id, creator_id=creator.id, status=TaskStatus.CANCELLED.value,
    )

    rows = await TasksRepository.list_archive(session)
    ids = {t.id for t in rows}
    assert done.id in ids
    assert cancelled.id in ids
    assert len(rows) == 2


async def test_list_archive_filter_status(session):
    dept_a, _, creator, ivanov, _ = await _seed(session)
    done = await _make_task(
        session, dept_id=dept_a.id, creator_id=creator.id,
        status=TaskStatus.DONE.value, completed_at=_utc(2026, 5, 10),
    )
    await _make_task(
        session, dept_id=dept_a.id, creator_id=creator.id,
        status=TaskStatus.CANCELLED.value,
    )

    only_done = await TasksRepository.list_archive(session, status="done")
    assert [t.id for t in only_done] == [done.id]


async def test_list_archive_filter_department_and_assignee(session):
    dept_a, dept_b, creator, ivanov, petrov = await _seed(session)
    t_a = await _make_task(
        session, dept_id=dept_a.id, creator_id=creator.id,
        assignee_id=ivanov.id, status=TaskStatus.DONE.value,
        completed_at=_utc(2026, 5, 1),
    )
    t_b = await _make_task(
        session, dept_id=dept_b.id, creator_id=creator.id,
        assignee_id=petrov.id, status=TaskStatus.DONE.value,
        completed_at=_utc(2026, 5, 2),
    )

    only_a = await TasksRepository.list_archive(session, department_id=dept_a.id)
    assert [t.id for t in only_a] == [t_a.id]

    only_petrov = await TasksRepository.list_archive(session, assignee_id=petrov.id)
    assert [t.id for t in only_petrov] == [t_b.id]


async def test_list_archive_filter_date_range(session):
    dept_a, _, creator, _, _ = await _seed(session)
    old = await _make_task(
        session, dept_id=dept_a.id, creator_id=creator.id,
        status=TaskStatus.DONE.value, completed_at=_utc(2026, 1, 1),
    )
    recent = await _make_task(
        session, dept_id=dept_a.id, creator_id=creator.id,
        status=TaskStatus.DONE.value, completed_at=_utc(2026, 5, 1),
    )

    in_may = await TasksRepository.list_archive(
        session,
        date_from=_utc(2026, 4, 1),
        date_to=_utc(2026, 6, 1),
    )
    ids = {t.id for t in in_may}
    assert recent.id in ids
    assert old.id not in ids


async def test_list_archive_pagination_and_ordering(session):
    dept_a, _, creator, _, _ = await _seed(session)
    # 12 done-задач с возрастающим completed_at
    ids: list[int] = []
    for i in range(12):
        t = await _make_task(
            session, dept_id=dept_a.id, creator_id=creator.id,
            status=TaskStatus.DONE.value,
            completed_at=_utc(2026, 5, 1, 12, i),
        )
        ids.append(t.id)

    total = await TasksRepository.count_archive(session)
    assert total == 12

    page0 = await TasksRepository.list_archive(session, limit=10, offset=0)
    page1 = await TasksRepository.list_archive(session, limit=10, offset=10)
    assert len(page0) == 10
    assert len(page1) == 2

    # сортировка completed_at DESC → самый поздний первым
    page0_ids = [t.id for t in page0]
    assert page0_ids[0] == ids[-1]
    assert page0_ids == sorted(page0_ids, reverse=True)


async def test_list_assignees_with_archive(session):
    dept_a, dept_b, creator, ivanov, petrov = await _seed(session)
    await _make_task(
        session, dept_id=dept_a.id, creator_id=creator.id,
        assignee_id=ivanov.id, status=TaskStatus.DONE.value,
        completed_at=_utc(2026, 5, 1),
    )
    await _make_task(
        session, dept_id=dept_b.id, creator_id=creator.id,
        assignee_id=petrov.id, status=TaskStatus.CANCELLED.value,
    )
    # active с ассайни — не должен попасть в архивных исполнителей
    await _make_task(
        session, dept_id=dept_a.id, creator_id=creator.id,
        assignee_id=ivanov.id, status=TaskStatus.IN_PROGRESS.value,
    )

    all_ass = set(await TasksRepository.list_assignees_with_archive(session))
    assert all_ass == {ivanov.id, petrov.id}

    only_a = set(
        await TasksRepository.list_assignees_with_archive(
            session, department_id=dept_a.id
        )
    )
    assert only_a == {ivanov.id}


async def test_get_full_loads_relations(session):
    dept_a, _, creator, ivanov, _ = await _seed(session)
    task = await _make_task(
        session, dept_id=dept_a.id, creator_id=creator.id,
        assignee_id=ivanov.id, status=TaskStatus.DONE.value,
        completed_at=_utc(2026, 5, 1),
    )
    full = await TasksRepository.get_full(session, task.id)
    assert full is not None
    assert full.creator.id == creator.id
    assert full.assignee is not None
    assert full.assignee.id == ivanov.id
    assert full.department.id == dept_a.id
    assert full.files == []
