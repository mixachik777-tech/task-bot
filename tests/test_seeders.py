from app.db.repositories.departments import DepartmentsRepository
from app.db.repositories.users import UsersRepository
from app.db.seeders import DEPT_FIXTURES, seed_admin, seed_departments


async def test_seed_departments_creates_three(session):
    await seed_departments(session)
    depts = await DepartmentsRepository.list_active(session)
    codes = {d.code for d in depts}
    assert codes == {code for code, _ in DEPT_FIXTURES}
    for d in depts:
        assert d.topic_id == 0


async def test_seed_departments_idempotent(session):
    await seed_departments(session)
    await seed_departments(session)
    depts = await DepartmentsRepository.list_active(session)
    assert len(depts) == 3


async def test_seed_admin_creates_admin_user(session):
    admin_tg = 12345
    await seed_admin(session, admin_tg)
    admin = await UsersRepository.get_by_tg_id(session, admin_tg)
    assert admin is not None
    assert admin.role == "admin"
    assert admin.is_active is True
    assert admin.full_name == "Admin"
    assert admin.department_id is None


async def test_seed_admin_idempotent(session):
    admin_tg = 54321
    await seed_admin(session, admin_tg)
    await seed_admin(session, admin_tg)
    matches = [
        u
        for u in await UsersRepository.list_active(session)
        if u.tg_user_id == admin_tg
    ]
    assert len(matches) == 1
