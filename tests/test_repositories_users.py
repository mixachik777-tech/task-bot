from app.db.enums import UserRole
from app.db.repositories.departments import DepartmentsRepository
from app.db.repositories.users import UsersRepository


async def test_create_and_get_by_tg_id(session):
    created = await UsersRepository.create(
        session,
        tg_user_id=111,
        full_name="Ivan Ivanov",
        tg_username="ivanov",
        role=UserRole.EMPLOYEE,
    )
    assert created.id is not None
    found = await UsersRepository.get_by_tg_id(session, 111)
    assert found is not None
    assert found.id == created.id
    assert found.full_name == "Ivan Ivanov"
    assert found.tg_username == "ivanov"
    assert found.role == UserRole.EMPLOYEE.value
    assert found.is_active is False


async def test_get_by_id_returns_none_for_missing(session):
    assert await UsersRepository.get_by_id(session, 9_999_999) is None


async def test_update_role_sets_both_role_and_department(session):
    dept = await DepartmentsRepository.create(session, code="dept_x", name="X", topic_id=0)
    user = await UsersRepository.create(session, tg_user_id=222, full_name="U")
    await UsersRepository.update_role(session, user.id, UserRole.LEAD, department_id=dept.id)
    await session.refresh(user)
    assert user.role == UserRole.LEAD.value
    assert user.department_id == dept.id


async def test_set_active_toggle(session):
    user = await UsersRepository.create(session, tg_user_id=333, full_name="U", is_active=False)
    await UsersRepository.set_active(session, user.id, True)
    await session.refresh(user)
    assert user.is_active is True
    await UsersRepository.set_active(session, user.id, False)
    await session.refresh(user)
    assert user.is_active is False


async def test_list_active_excludes_inactive(session):
    await UsersRepository.create(session, tg_user_id=444, full_name="A", is_active=True)
    await UsersRepository.create(session, tg_user_id=445, full_name="B", is_active=False)
    active = await UsersRepository.list_active(session)
    tg_ids = {u.tg_user_id for u in active}
    assert 444 in tg_ids
    assert 445 not in tg_ids


async def test_list_pending_approval(session):
    await UsersRepository.create(session, tg_user_id=555, full_name="A", is_active=True)
    await UsersRepository.create(session, tg_user_id=556, full_name="B", is_active=False)
    pending = await UsersRepository.list_pending_approval(session)
    tg_ids = {u.tg_user_id for u in pending}
    assert 555 not in tg_ids
    assert 556 in tg_ids


async def test_list_by_department(session):
    d1 = await DepartmentsRepository.create(session, code="d1", name="D1", topic_id=0)
    d2 = await DepartmentsRepository.create(session, code="d2", name="D2", topic_id=0)
    await UsersRepository.create(session, tg_user_id=666, full_name="A", department_id=d1.id)
    await UsersRepository.create(session, tg_user_id=667, full_name="B", department_id=d2.id)
    await UsersRepository.create(session, tg_user_id=668, full_name="C", department_id=d1.id)
    in_d1 = await UsersRepository.list_by_department(session, d1.id)
    assert {u.tg_user_id for u in in_d1} == {666, 668}


async def test_list_approved_in_department_filters(session):
    """Возвращает только активных approved-сотрудников отдела + умеет
    исключать заданного user_id (текущий assignee)."""
    from sqlalchemy import update as _upd
    from app.db.models import User as _U

    async def _mk(tg, name, *, dept, active, approved):
        u = await UsersRepository.create(
            session,
            tg_user_id=tg,
            full_name=name,
            department_id=dept,
            is_active=active,
        )
        await session.execute(
            _upd(_U)
            .where(_U.id == u.id)
            .values(access_status="approved" if approved else "pending")
        )
        await session.flush()
        return u

    d = await DepartmentsRepository.create(session, code="d_re", name="Reassign", topic_id=0)
    u_ok1 = await _mk(900001, "Алёна", dept=d.id, active=True, approved=True)
    u_ok2 = await _mk(900002, "Борис", dept=d.id, active=True, approved=True)
    # неактивный — не войдёт
    await _mk(900003, "Вика", dept=d.id, active=False, approved=True)
    # pending — не войдёт
    await _mk(900004, "Гена", dept=d.id, active=True, approved=False)
    # из другого отдела — не войдёт
    d2 = await DepartmentsRepository.create(session, code="d_other", name="Other", topic_id=0)
    await _mk(900005, "Дима", dept=d2.id, active=True, approved=True)

    # без exclude — оба подходящих
    candidates = await UsersRepository.list_approved_in_department(
        session,
        department_id=d.id,
    )
    assert {u.tg_user_id for u in candidates} == {900001, 900002}

    # exclude=u_ok1 → остаётся только u_ok2
    candidates2 = await UsersRepository.list_approved_in_department(
        session,
        department_id=d.id,
        exclude_user_id=u_ok1.id,
    )
    assert [u.id for u in candidates2] == [u_ok2.id]

    # exclude обоих → пусто (нельзя передать никому)
    candidates3 = await UsersRepository.list_approved_in_department(
        session,
        department_id=d.id,
        exclude_user_id=u_ok1.id,
    )
    candidates3 = [u for u in candidates3 if u.id != u_ok2.id]
    assert candidates3 == []
