from app.db.repositories.app_settings import (
    KEY_LEADERSHIP_TOPIC_ID,
    KEY_TASK_CHAT_ID,
    AppSettingsRepository,
)


async def test_get_returns_none_for_missing(session):
    assert await AppSettingsRepository.get(session, "no_such_key") is None
    assert await AppSettingsRepository.get_int(session, "no_such_key") is None


async def test_set_then_get(session):
    await AppSettingsRepository.set(session, KEY_TASK_CHAT_ID, "-1001234567890")
    val = await AppSettingsRepository.get(session, KEY_TASK_CHAT_ID)
    assert val == "-1001234567890"


async def test_set_is_upsert(session):
    await AppSettingsRepository.set(session, KEY_TASK_CHAT_ID, "111")
    await AppSettingsRepository.set(session, KEY_TASK_CHAT_ID, "222")
    val = await AppSettingsRepository.get(session, KEY_TASK_CHAT_ID)
    assert val == "222"


async def test_get_int_parses_value(session):
    await AppSettingsRepository.set(session, KEY_LEADERSHIP_TOPIC_ID, "42")
    assert await AppSettingsRepository.get_int(session, KEY_LEADERSHIP_TOPIC_ID) == 42


async def test_get_int_returns_none_for_non_integer(session):
    await AppSettingsRepository.set(session, "junk", "abc")
    assert await AppSettingsRepository.get_int(session, "junk") is None
