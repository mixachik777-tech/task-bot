"""
Pytest-фикстуры для unit-тестов репозиториев и сидеров.

Контракт:
- postgres_container (session) — pgvector/pgvector:pg16 (тот же образ, что в проде).
- _patch_settings_db (session, autouse) — перенаправляет settings.db_dsn на тестовый
  контейнер, чтобы alembic env.py и наш engine читали правильные координаты БД.
- apply_migrations (session, autouse) — alembic command.upgrade("head") один раз на сессию.
- engine (session) — async-движок против тестового контейнера.
- session (function) — AsyncSession с join_transaction_mode="create_savepoint"
  внутри внешней транзакции, которая всегда откатывается → каждый тест видит чистое
  состояние.
"""

from typing import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool
from testcontainers.postgres import PostgresContainer

from app.config import settings


@pytest.fixture(scope="session")
def postgres_container() -> Iterator[PostgresContainer]:
    container = PostgresContainer(
        image="pgvector/pgvector:pg16",
        username="taskbot",
        password="taskbot",
        dbname="taskbot",
    )
    container.start()
    try:
        yield container
    finally:
        container.stop()


@pytest.fixture(scope="session", autouse=True)
def _patch_settings_db(postgres_container: PostgresContainer) -> Iterator[None]:
    orig = {
        "DB_HOST": settings.DB_HOST,
        "DB_PORT": settings.DB_PORT,
        "DB_USER": settings.DB_USER,
        "DB_PASSWORD": settings.DB_PASSWORD,
        "DB_NAME": settings.DB_NAME,
    }
    settings.DB_HOST = postgres_container.get_container_host_ip()
    settings.DB_PORT = int(postgres_container.get_exposed_port(5432))
    settings.DB_USER = postgres_container.username
    settings.DB_PASSWORD = postgres_container.password
    settings.DB_NAME = postgres_container.dbname
    try:
        yield
    finally:
        for k, v in orig.items():
            setattr(settings, k, v)


@pytest.fixture(scope="session", autouse=True)
def apply_migrations(_patch_settings_db: None) -> None:
    cfg = Config("alembic.ini")
    command.upgrade(cfg, "head")


@pytest_asyncio.fixture(scope="session")
async def engine():
    eng = create_async_engine(settings.db_dsn, poolclass=NullPool)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest_asyncio.fixture()
async def session(engine, apply_migrations) -> AsyncIterator[AsyncSession]:
    async with engine.connect() as conn:
        outer_tx = await conn.begin()
        async_session = AsyncSession(
            bind=conn,
            join_transaction_mode="create_savepoint",
            expire_on_commit=False,
        )
        try:
            yield async_session
        finally:
            await async_session.close()
            await outer_tx.rollback()
