from app.db.repositories.departments import DepartmentsRepository


async def test_create_and_get_by_code(session):
    created = await DepartmentsRepository.create(
        session, code="alpha", name="Alpha", topic_id=42
    )
    assert created.id is not None
    found = await DepartmentsRepository.get_by_code(session, "alpha")
    assert found is not None
    assert found.id == created.id
    assert found.name == "Alpha"
    assert found.topic_id == 42


async def test_get_by_id(session):
    created = await DepartmentsRepository.create(
        session, code="beta", name="Beta"
    )
    found = await DepartmentsRepository.get_by_id(session, created.id)
    assert found is not None
    assert found.code == "beta"
    assert await DepartmentsRepository.get_by_id(session, 9_999_999) is None


async def test_list_active_excludes_inactive(session):
    await DepartmentsRepository.create(session, code="d_active", name="Active")
    inactive = await DepartmentsRepository.create(
        session, code="d_inactive", name="Inactive"
    )
    inactive.is_active = False
    await session.flush()

    active = await DepartmentsRepository.list_active(session)
    codes = {d.code for d in active}
    assert "d_active" in codes
    assert "d_inactive" not in codes


async def test_update_topic_id(session):
    created = await DepartmentsRepository.create(
        session, code="gamma", name="Gamma", topic_id=0
    )
    await DepartmentsRepository.update_topic_id(session, created.id, 999)
    await session.refresh(created)
    assert created.topic_id == 999
