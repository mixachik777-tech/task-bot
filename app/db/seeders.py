"""
Сидер initial-данных: 3 отдела + админ из ADMIN_TG_ID.

Идемпотентен (повторный запуск не дублирует записи). Запускается из
docker-entrypoint.sh ПОСЛЕ alembic upgrade head, ДО старта бота.
Вся работа в одной транзакции — при любой ошибке полный rollback.
"""

import asyncio

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.base import async_session_factory, engine
from app.db.enums import UserRole
from app.db.repositories.app_settings import (
    KEY_ONBOARDING_APPROVER_TG_ID,
    AppSettingsRepository,
)
from app.db.repositories.departments import DepartmentsRepository
from app.db.repositories.users import UsersRepository

DEPT_FIXTURES: list[tuple[str, str]] = [
    ("correctors", "Корректоры"),
    ("designers", "Дизайнеры"),
    ("correspondents", "Корреспонденты"),
]

# Дефолтный аппрувер onboarding-запросов — @shaminaa_a (Алёна).
# Можно переопределить через прямой UPDATE app_settings, не задевая код.
DEFAULT_ONBOARDING_APPROVER_TG_ID = 409716754


async def seed_departments(session: AsyncSession) -> None:
    for code, name in DEPT_FIXTURES:
        existing = await DepartmentsRepository.get_by_code(session, code)
        if existing is None:
            await DepartmentsRepository.create(
                session, code=code, name=name, topic_id=0
            )
            logger.info("Сидер: создан отдел {} ({})", code, name)
        else:
            logger.debug("Сидер: отдел {} уже существует, пропуск", code)


async def seed_admin(session: AsyncSession, admin_tg_id: int) -> None:
    existing = await UsersRepository.get_by_tg_id(session, admin_tg_id)
    if existing is None:
        await UsersRepository.create(
            session,
            tg_user_id=admin_tg_id,
            full_name="Admin",
            role=UserRole.ADMIN,
            department_id=None,
            is_active=True,
        )
        logger.info("Сидер: создан админ tg_user_id={}", admin_tg_id)
    else:
        logger.debug(
            "Сидер: админ tg_user_id={} уже существует, пропуск", admin_tg_id
        )


async def seed_onboarding_approver(session: AsyncSession) -> None:
    """Один раз ставит дефолтного аппрувера, если ключа в app_settings ещё нет."""
    existing = await AppSettingsRepository.get(
        session, KEY_ONBOARDING_APPROVER_TG_ID
    )
    if existing is None:
        await AppSettingsRepository.set(
            session,
            KEY_ONBOARDING_APPROVER_TG_ID,
            str(DEFAULT_ONBOARDING_APPROVER_TG_ID),
        )
        logger.info(
            "Сидер: ONBOARDING_APPROVER_TG_ID={}",
            DEFAULT_ONBOARDING_APPROVER_TG_ID,
        )
    else:
        logger.debug(
            "Сидер: ONBOARDING_APPROVER_TG_ID уже задан ({}), пропуск",
            existing,
        )


async def run_seeders() -> None:
    logger.info("Сидер: старт (ADMIN_TG_ID={})", settings.ADMIN_TG_ID)
    async with async_session_factory() as session:
        async with session.begin():
            await seed_departments(session)
            await seed_admin(session, settings.ADMIN_TG_ID)
            await seed_onboarding_approver(session)
    await engine.dispose()
    logger.info("Сидер: завершён")


if __name__ == "__main__":
    asyncio.run(run_seeders())
