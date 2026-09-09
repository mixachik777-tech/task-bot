"""
Юнит-тесты помощников TaskActionsService — чистые, без БД.

Полный e2e-flow через TaskActionsService требует общей БД между тестовой
сессией и сессией, которую сервис открывает через async_session_factory
(сейчас они изолированы через savepoint в conftest). Переход на
session-injection в сервисе — задача BACKLOG для Этапа 5+/6.

На Этапе 5 покрываем тестами:
- _assert_transition: разрешённые и запрещённые переходы статуса
- _can_complete / _can_cancel / _can_comment: permission-матрица
- ActionResult: dataclass поведение

Полный flow accept/complete/cancel/comment проверяется smoke-тестом
в docker (ручная приёмка) + через TasksRepository.lock_for_update
(тест существует в test_repositories_tasks.py).
"""

import pytest

from app.db.enums import TaskStatus, UserRole
from app.db.exceptions import InvalidStatusTransition
from app.db.models import Task, User
from app.services.task_actions import (
    ActionResult,
    _assert_transition,
    _can_cancel,
    _can_comment,
    _can_complete,
    _user_handle,
)


# ──────────────────────────────────────────────────────────────────────
# _assert_transition
# ──────────────────────────────────────────────────────────────────────


def test_assert_transition_allowed_new_to_in_progress():
    _assert_transition("new", TaskStatus.IN_PROGRESS)  # без исключений


def test_assert_transition_allowed_in_progress_to_awaiting_approval():
    # Этап Б: прямой переход in_progress → done закрыт; теперь только
    # через awaiting_approval (согласование постановщиком).
    _assert_transition("in_progress", TaskStatus.AWAITING_APPROVAL)


def test_assert_transition_allowed_awaiting_approval_to_done():
    _assert_transition("awaiting_approval", TaskStatus.DONE)


def test_assert_transition_blocked_in_progress_to_done_direct():
    with pytest.raises(InvalidStatusTransition):
        _assert_transition("in_progress", TaskStatus.DONE)


def test_assert_transition_allowed_new_to_cancelled():
    _assert_transition("new", TaskStatus.CANCELLED)


def test_assert_transition_allowed_in_progress_to_cancelled():
    _assert_transition("in_progress", TaskStatus.CANCELLED)


def test_assert_transition_blocked_done_to_in_progress():
    with pytest.raises(InvalidStatusTransition):
        _assert_transition("done", TaskStatus.IN_PROGRESS)


def test_assert_transition_blocked_cancelled_to_done():
    with pytest.raises(InvalidStatusTransition):
        _assert_transition("cancelled", TaskStatus.DONE)


def test_assert_transition_blocked_new_to_done():
    with pytest.raises(InvalidStatusTransition):
        _assert_transition("new", TaskStatus.DONE)


def test_assert_transition_unknown_status_raises():
    with pytest.raises(InvalidStatusTransition):
        _assert_transition("garbage", TaskStatus.DONE)


# ──────────────────────────────────────────────────────────────────────
# Permission predicates
# ──────────────────────────────────────────────────────────────────────


def _user(*, id: int, role: str = UserRole.EMPLOYEE.value) -> User:
    return User(
        id=id,
        tg_user_id=10000 + id,
        full_name=f"U{id}",
        tg_username=None,
        role=role,
        is_active=True,
    )


def _task(*, creator_id: int, assignee_id: int | None = None) -> Task:
    t = Task(
        id=42,
        title="t",
        description=None,
        priority="medium",
        status="new",
        deadline=None,  # не нужно для permission-проверок
        creator_id=creator_id,
        assignee_id=assignee_id,
        department_id=1,
    )
    return t


def test_can_complete_assignee_yes():
    task = _task(creator_id=1, assignee_id=2)
    assert _can_complete(task, _user(id=2)) is True


def test_can_complete_admin_yes_even_if_not_assignee():
    task = _task(creator_id=1, assignee_id=2)
    assert _can_complete(task, _user(id=99, role=UserRole.ADMIN.value)) is True


def test_can_complete_creator_no():
    task = _task(creator_id=1, assignee_id=2)
    assert _can_complete(task, _user(id=1)) is False


def test_can_complete_other_no():
    task = _task(creator_id=1, assignee_id=2)
    assert _can_complete(task, _user(id=3)) is False


def test_can_cancel_creator_yes():
    task = _task(creator_id=1, assignee_id=2)
    assert _can_cancel(task, _user(id=1)) is True


def test_can_cancel_admin_yes():
    task = _task(creator_id=1)
    assert _can_cancel(task, _user(id=99, role=UserRole.ADMIN.value)) is True


def test_can_cancel_assignee_no():
    task = _task(creator_id=1, assignee_id=2)
    assert _can_cancel(task, _user(id=2)) is False


def test_can_cancel_other_no():
    task = _task(creator_id=1)
    assert _can_cancel(task, _user(id=3)) is False


def test_can_comment_creator_yes():
    task = _task(creator_id=1, assignee_id=2)
    assert _can_comment(task, _user(id=1)) is True


def test_can_comment_assignee_yes():
    task = _task(creator_id=1, assignee_id=2)
    assert _can_comment(task, _user(id=2)) is True


def test_can_comment_admin_yes():
    task = _task(creator_id=1, assignee_id=2)
    assert _can_comment(task, _user(id=99, role=UserRole.ADMIN.value)) is True


def test_can_comment_lead_yes():
    task = _task(creator_id=1, assignee_id=2)
    assert _can_comment(task, _user(id=10, role=UserRole.LEAD.value)) is True


def test_can_comment_other_employee_no():
    task = _task(creator_id=1, assignee_id=2)
    assert _can_comment(task, _user(id=3)) is False


# ──────────────────────────────────────────────────────────────────────
# ActionResult & helpers
# ──────────────────────────────────────────────────────────────────────


def test_action_result_defaults():
    r = ActionResult(success=True, user_message="ok")
    assert r.task is None
    assert r.notifications == []


def test_user_handle_with_username():
    u = User(id=1, tg_user_id=10, full_name="N", tg_username="ivanov")
    assert _user_handle(u) == "@ivanov"


def test_user_handle_without_username_uses_full_name():
    u = User(id=1, tg_user_id=10, full_name="Иван Иванов", tg_username=None)
    assert _user_handle(u) == "Иван Иванов"


def test_user_handle_none_returns_dash():
    assert _user_handle(None) == "—"
