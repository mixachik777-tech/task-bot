"""
End-to-end сценарии TaskActionsService на реальной БД через testcontainer.

Покрывают регрессии, которые были найдены 2026-06-02:
- outbox.enqueue_idempotent падал на рассинхроне partial-index'a — accept/
  complete/cancel/approve откатывались внутри session.begin(),
- _sync_card сидел ВНУТРИ session.begin() и слал edit_message_text в TG
  раньше commit'a — при любом rollback'е получался рассинхрон TG↔БД.

Тесты подменяют module-level async_session_factory на factory, который
смотрит в testcontainer postgres (тот же, что conftest.py использует для
тестов репозиториев). Bot mock'нут AsyncMock'ом — реальных TG-вызовов нет.
После каждого теста делается TRUNCATE всех связанных таблиц.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.db.base as db_base
from app.db.enums import TaskPriority, TaskStatus, UserRole
from app.db.repositories.departments import DepartmentsRepository
from app.db.repositories.tasks import TasksRepository
from app.db.repositories.users import UsersRepository
from app.services.task_actions import TaskActionsService


@pytest_asyncio.fixture()
async def service_engine(engine):
    """Используем тот же engine, что conftest.engine — он уже инициализирован
    после _patch_settings_db (через зависимости тестов репозиториев). Свой
    engine с asyncpg падал на 'Name or service not known' при создании в
    отдельном event-loop'е, хотя dsn был корректным; причина в кешировании
    asyncpg getaddrinfo. Переиспользование engine из conftest эту проблему
    обходит."""
    yield engine


@pytest_asyncio.fixture(autouse=True)
async def _swap_session_factory(service_engine):
    """Подменяем async_session_factory в namespace модуля сервиса.
    task_actions делает `from app.db.base import async_session_factory`,
    что замораживает ссылку на оригинальный объект в его namespace.
    Patch `app.db.base.async_session_factory = ...` сюда не доходит —
    приходится патчить КАЖДЫЙ модуль, который импортирует фабрику."""
    import app.services.task_actions as ta_mod

    test_factory = async_sessionmaker(service_engine, expire_on_commit=False)
    orig_base = db_base.async_session_factory
    orig_ta = ta_mod.async_session_factory
    db_base.async_session_factory = test_factory
    ta_mod.async_session_factory = test_factory
    yield
    db_base.async_session_factory = orig_base
    ta_mod.async_session_factory = orig_ta


@pytest_asyncio.fixture(autouse=True)
async def _truncate_between_tests(service_engine):
    """Чистим БД после каждого теста. CASCADE подхватит зависимости
    (task_outbox, task_broadcasts, task_history, task_files → tasks)."""
    yield
    async with service_engine.connect() as conn:
        await conn.execute(
            text(
                "TRUNCATE TABLE tasks, task_history, task_outbox, "
                "task_broadcasts, task_files, users, departments "
                "RESTART IDENTITY CASCADE"
            )
        )
        await conn.commit()


@pytest_asyncio.fixture()
async def db(service_engine):
    """Сессия для подготовки fixture-данных. Тесты сами вызывают commit."""
    factory = async_sessionmaker(service_engine, expire_on_commit=False)
    async with factory() as session:
        yield session
        await session.rollback()


@pytest.fixture()
def fake_bot():
    """Bot mock с AsyncMock-методами. Любой await bot.X(...) возвращает
    AsyncMock без сетевых вызовов в Telegram."""
    bot = AsyncMock()
    bot.edit_message_text = AsyncMock(return_value=None)
    bot.send_message = AsyncMock(return_value=AsyncMock(message_id=999))
    bot.delete_message = AsyncMock(return_value=None)
    return bot


def _future_dt(hours: int = 24) -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=hours)


async def _seed(db, *, with_executor: bool = True):
    """Минимальный сидер: 1 department + creator (employee) + executor (employee).
    Используется фабрикой задач во всех e2e сценариях."""
    dept = await DepartmentsRepository.create(db, code="e2e_dept", name="E2E", topic_id=0)
    creator = await UsersRepository.create(
        db,
        tg_user_id=1001,
        full_name="Creator",
        is_active=True,
        department_id=dept.id,
        role=UserRole.EMPLOYEE,
    )
    executor = None
    if with_executor:
        executor = await UsersRepository.create(
            db,
            tg_user_id=1002,
            full_name="Executor",
            is_active=True,
            department_id=dept.id,
            role=UserRole.EMPLOYEE,
        )
    return dept, creator, executor


async def _make_task(db, dept_id: int, creator_id: int, *, title: str = "Test"):
    return await TasksRepository.create(
        db,
        title=title,
        description="d",
        priority=TaskPriority.MEDIUM,
        deadline=_future_dt(48),
        creator_id=creator_id,
        department_id=dept_id,
    )


# ──────────────────────────────────────────────────────────────────────
# Полный цикл: accept → complete → approve
# ──────────────────────────────────────────────────────────────────────


async def test_full_cycle_accept_complete_approve(db, fake_bot):
    """Регрессия на outbox.enqueue_idempotent — раньше падал внутри
    транзакции на каждом из 3 переходов. Если фикс index_where жив —
    весь цикл проходит без ProgrammingError, задача в done."""
    dept, creator, executor = await _seed(db)
    task = await _make_task(db, dept.id, creator.id, title="Full cycle")
    await db.commit()

    # accept (executor)
    with patch("app.services.task_actions._try_update_lead_mirror"):
        r1 = await TaskActionsService.accept(bot=fake_bot, task_id=task.id, user=executor)
    assert r1.success, r1.user_message
    assert r1.task is not None
    assert r1.task.status == TaskStatus.IN_PROGRESS.value
    assert r1.task.assignee_id == executor.id

    # complete (executor)
    with (
        patch("app.services.task_actions._try_update_lead_mirror"),
        patch("app.services.task_actions._try_post_event_to_lead"),
        patch("app.services.task_actions._drop_reminders"),
    ):
        r2 = await TaskActionsService.complete(
            bot=fake_bot,
            task_id=task.id,
            user=executor,
            result_comment="готово",
            result_files=[],
        )
    assert r2.success, r2.user_message
    assert r2.task.status == TaskStatus.AWAITING_APPROVAL.value

    # approve (creator)
    with (
        patch("app.services.task_actions._try_update_lead_mirror"),
        patch("app.services.task_actions._try_purge_overdue"),
        patch("app.services.task_actions._drop_reminders"),
    ):
        r3 = await TaskActionsService.approve(bot=fake_bot, task_id=task.id, user=creator)
    assert r3.success, r3.user_message
    assert r3.task.status == TaskStatus.DONE.value
    assert r3.task.completed_at is not None


# ──────────────────────────────────────────────────────────────────────
# cancel из new — обходит accept (как Шамина-admin на чужой задаче)
# ──────────────────────────────────────────────────────────────────────


async def test_cancel_new_task_by_admin(db, fake_bot):
    dept, creator, _ = await _seed(db, with_executor=False)
    admin = await UsersRepository.create(
        db,
        tg_user_id=1003,
        full_name="Admin",
        is_active=True,
        department_id=dept.id,
        role=UserRole.ADMIN,
    )
    task = await _make_task(db, dept.id, creator.id, title="To cancel")
    await db.commit()

    with (
        patch("app.services.task_actions._try_update_lead_mirror"),
        patch("app.services.task_actions._try_purge_overdue"),
        patch("app.services.task_actions._drop_reminders"),
    ):
        r = await TaskActionsService.cancel(bot=fake_bot, task_id=task.id, user=admin)
    assert r.success, r.user_message
    assert r.task.status == TaskStatus.CANCELLED.value


# ──────────────────────────────────────────────────────────────────────
# Регрессия: TG-edit ПОСЛЕ commit, не до
# ──────────────────────────────────────────────────────────────────────


async def test_tg_edit_called_only_after_commit(db, fake_bot):
    """Если у задачи есть dept_chat_id+dept_message_id — accept должен
    после commit'a сделать ровно один edit_message_text. Если фикс
    «snapshot + apply after commit» жив — вызов произойдёт ПОСЛЕ изменения
    БД, и БД-status будет ровно in_progress на момент вызова."""
    dept, creator, executor = await _seed(db)
    task = await _make_task(db, dept.id, creator.id, title="TG sync")
    # Симулируем DM-режим: задача уже broadcast'нута, у executor'а карточка
    task.dept_chat_id = 1002  # tg_user_id executor'а
    task.dept_message_id = 12345
    db.add(task)
    await db.commit()

    with patch("app.services.task_actions._try_update_lead_mirror"):
        r = await TaskActionsService.accept(bot=fake_bot, task_id=task.id, user=executor)
    assert r.success, r.user_message
    # Должен быть ровно один вызов edit_message_text — на live-карточке
    assert fake_bot.edit_message_text.await_count == 1
    call_kwargs = fake_bot.edit_message_text.await_args.kwargs
    assert call_kwargs["chat_id"] == 1002
    assert call_kwargs["message_id"] == 12345


# ──────────────────────────────────────────────────────────────────────
# Прицельная регрессия: outbox.enqueue_idempotent и partial-index predicate.
# Полный e2e через accept→complete→approve не ловит этот баг, потому что
# каждый шаг делает ровно один INSERT и до ON CONFLICT-ветки не доходит.
# Здесь явно дважды зовём enqueue_idempotent на одной (task_id, action),
# чтобы заставить Postgres матчить predicate индекса. Если index_where в
# коде не совпадает буква-в-букву с миграцией (status::text='pending'::text),
# второй вызов падает InvalidColumnReferenceError ещё до выполнения INSERT.
# ──────────────────────────────────────────────────────────────────────


async def test_enqueue_idempotent_partial_index_match(db):
    from sqlalchemy import select

    from app.db.models import TaskOutbox
    from app.db.repositories.outbox import OutboxRepository

    dept, creator, _ = await _seed(db, with_executor=False)
    task = await _make_task(db, dept.id, creator.id, title="Outbox idem")
    await db.commit()

    await OutboxRepository.enqueue_idempotent(db, task.id, "update_lead")
    await OutboxRepository.enqueue_idempotent(db, task.id, "update_lead")
    await db.commit()

    result = await db.execute(
        select(TaskOutbox).where(TaskOutbox.task_id == task.id, TaskOutbox.action == "update_lead")
    )
    rows = result.scalars().all()
    assert len(rows) == 1
    assert rows[0].status == "pending"
