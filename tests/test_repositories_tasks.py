from datetime import datetime, timedelta, timezone

from app.db.enums import HistoryEventType, TaskPriority, TaskStatus
from app.db.repositories.departments import DepartmentsRepository
from app.db.repositories.history import HistoryRepository
from app.db.repositories.tasks import TasksRepository
from app.db.repositories.users import UsersRepository


async def _seed_minimum(session):
    dept = await DepartmentsRepository.create(
        session, code="t_dept", name="TDept", topic_id=0
    )
    user = await UsersRepository.create(
        session, tg_user_id=10001, full_name="Creator", is_active=True
    )
    return dept, user


def _future_dt(hours: int = 24) -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=hours)


async def test_create_task_basic_fields(session):
    dept, creator = await _seed_minimum(session)
    deadline = _future_dt(48)
    task = await TasksRepository.create(
        session,
        title="Test task",
        description="desc",
        priority=TaskPriority.HIGH,
        deadline=deadline,
        creator_id=creator.id,
        department_id=dept.id,
    )
    assert task.id is not None
    assert task.status == TaskStatus.NEW.value
    assert task.priority == TaskPriority.HIGH.value
    assert task.creator_id == creator.id
    assert task.department_id == dept.id
    fetched = await TasksRepository.get_by_id(session, task.id)
    assert fetched is not None
    assert fetched.title == "Test task"


async def test_add_files(session):
    dept, creator = await _seed_minimum(session)
    task = await TasksRepository.create(
        session,
        title="With files",
        description=None,
        priority=TaskPriority.LOW,
        deadline=_future_dt(),
        creator_id=creator.id,
        department_id=dept.id,
    )
    await TasksRepository.add_files(
        session,
        task.id,
        [
            {
                "file_id": "fid1",
                "unique_id": "u1",
                "file_name": "a.pdf",
                "size": 12345,
                "mime_type": "application/pdf",
            },
            {
                "file_id": "fid2",
                "unique_id": "u2",
                "file_name": "b.png",
                "size": 678,
                "mime_type": "image/png",
            },
        ],
    )
    full = await TasksRepository.get_with_files(session, task.id)
    assert full is not None
    assert len(full.files) == 2
    names = {f.file_name for f in full.files}
    assert names == {"a.pdf", "b.png"}


async def test_set_message_ids(session):
    dept, creator = await _seed_minimum(session)
    task = await TasksRepository.create(
        session,
        title="Posted",
        description=None,
        priority=TaskPriority.MEDIUM,
        deadline=_future_dt(),
        creator_id=creator.id,
        department_id=dept.id,
    )
    await TasksRepository.set_message_ids(
        session, task.id,
        dept_chat_id=-1001,
        dept_message_id=42,
        arch_message_id=43,
    )
    await session.refresh(task)
    assert task.dept_chat_id == -1001
    assert task.dept_message_id == 42
    assert task.arch_message_id == 43


async def test_list_unassigned_returns_only_new_without_assignee(session):
    d1, creator = await _seed_minimum(session)
    ivanov = await UsersRepository.create(
        session, tg_user_id=10042, full_name="Ivan", is_active=True,
    )
    # свободная задача — должна попасть
    free = await TasksRepository.create(
        session, title="Free", description=None, priority=TaskPriority.LOW,
        deadline=_future_dt(48), creator_id=creator.id, department_id=d1.id,
    )
    # уже взятая (assignee назначен) — не должна попасть
    taken = await TasksRepository.create(
        session, title="Taken", description=None, priority=TaskPriority.LOW,
        deadline=_future_dt(24), creator_id=creator.id, department_id=d1.id,
    )
    from sqlalchemy import update as _upd
    from app.db.models import Task as _Task
    await session.execute(
        _upd(_Task).where(_Task.id == taken.id).values(
            assignee_id=ivanov.id, status=TaskStatus.IN_PROGRESS.value,
        )
    )
    await session.flush()

    result = await TasksRepository.list_unassigned(session)
    ids = {t.id for t in result}
    assert free.id in ids
    assert taken.id not in ids


async def test_list_active_filters_by_department_and_status(session):
    d1, creator = await _seed_minimum(session)
    d2 = await DepartmentsRepository.create(
        session, code="t_dept_2", name="TDept2", topic_id=0
    )
    t1 = await TasksRepository.create(
        session, title="A", description=None, priority=TaskPriority.LOW,
        deadline=_future_dt(1), creator_id=creator.id, department_id=d1.id,
    )
    await TasksRepository.create(
        session, title="B", description=None, priority=TaskPriority.LOW,
        deadline=_future_dt(2), creator_id=creator.id, department_id=d2.id,
    )
    in_d1 = await TasksRepository.list_active(session, department_id=d1.id)
    assert {t.id for t in in_d1} == {t1.id}


async def test_lock_for_update_returns_task(session):
    dept, creator = await _seed_minimum(session)
    task = await TasksRepository.create(
        session, title="L", description=None, priority=TaskPriority.LOW,
        deadline=_future_dt(), creator_id=creator.id, department_id=dept.id,
    )
    locked = await TasksRepository.lock_for_update(session, task.id)
    assert locked is not None
    assert locked.id == task.id


async def test_lock_for_update_missing(session):
    assert await TasksRepository.lock_for_update(session, 9_999_999) is None


async def test_count_files(session):
    dept, creator = await _seed_minimum(session)
    task = await TasksRepository.create(
        session, title="CF", description=None, priority=TaskPriority.LOW,
        deadline=_future_dt(), creator_id=creator.id, department_id=dept.id,
    )
    assert await TasksRepository.count_files(session, task.id) == 0
    await TasksRepository.add_files(
        session, task.id,
        [
            {"file_id": "a", "unique_id": "ua", "file_name": "a", "size": 1, "mime_type": None},
            {"file_id": "b", "unique_id": "ub", "file_name": "b", "size": 2, "mime_type": None},
        ],
    )
    assert await TasksRepository.count_files(session, task.id) == 2


async def test_history_log_and_list_with_tiebreaker(session):
    dept, creator = await _seed_minimum(session)
    task = await TasksRepository.create(
        session, title="H", description=None, priority=TaskPriority.LOW,
        deadline=_future_dt(), creator_id=creator.id, department_id=dept.id,
    )
    h1 = await HistoryRepository.log(
        session, task_id=task.id, user_id=creator.id,
        event_type=HistoryEventType.CREATED,
    )
    h2 = await HistoryRepository.log(
        session, task_id=task.id, user_id=creator.id,
        event_type=HistoryEventType.COMMENTED, payload={"text": "ok"},
    )
    events = await HistoryRepository.list_by_task(session, task.id)
    assert [e.id for e in events] == [h1.id, h2.id]
    assert events[1].event_type == HistoryEventType.COMMENTED.value
    assert events[1].payload == {"text": "ok"}


# ──────────────────────────────────────────────────────────────────────
# display_number — сквозная плотная нумерация
# ──────────────────────────────────────────────────────────────────────


async def test_display_number_starts_from_one(session):
    dept, creator = await _seed_minimum(session)
    task = await TasksRepository.create(
        session, title="A", description=None, priority=TaskPriority.LOW,
        deadline=_future_dt(), creator_id=creator.id, department_id=dept.id,
    )
    assert task.display_number == 1


async def test_display_number_increments_density(session):
    """Несколько задач подряд получают плотные номера 1..N (без дыр).

    В реальном проде sequence на id может оставлять дыры от rollback'ов,
    но display_number — отдельный счётчик MAX+1 под advisory-lock.
    """
    dept, creator = await _seed_minimum(session)
    nums = []
    for _ in range(5):
        t = await TasksRepository.create(
            session, title="x", description=None, priority=TaskPriority.LOW,
            deadline=_future_dt(), creator_id=creator.id, department_id=dept.id,
        )
        nums.append(t.display_number)
    assert nums == [1, 2, 3, 4, 5]


async def test_get_display_number_lookup(session):
    dept, creator = await _seed_minimum(session)
    task = await TasksRepository.create(
        session, title="L", description=None, priority=TaskPriority.LOW,
        deadline=_future_dt(), creator_id=creator.id, department_id=dept.id,
    )
    found = await TasksRepository.get_display_number(session, task.id)
    assert found == task.display_number
    missing = await TasksRepository.get_display_number(session, 99999999)
    assert missing is None
