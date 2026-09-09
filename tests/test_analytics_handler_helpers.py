"""
Регрессионные тесты для helper-функций analytics handler.
Покрывают баги, вскрытые ручным тестированием 2026-05-17:

A) `_format_avg` не должен возвращать строки с угловой скобкой (HTML-парс).
B) `_allowed_scopes` для admin без department_id не должен включать `department`.
C) `_safe_scope` нормализует невалидный сохранённый scope.
"""

from app.bot.handlers.analytics import (
    _allowed_scopes,
    _format_avg,
    _safe_scope,
)
from app.db.enums import UserRole
from app.db.models import User


def _u(role: str, *, dept: int | None = None) -> User:
    return User(
        id=1,
        tg_user_id=1,
        tg_username=None,
        full_name="Test",
        role=role,
        department_id=dept,
        is_active=True,
    )


# ---- Баг A: HTML escape ----


def test_format_avg_no_angle_bracket_under_minute():
    out = _format_avg(30)
    assert "<" not in out
    assert "менее минуты" in out


def test_format_avg_none_returns_dash():
    assert _format_avg(None) == "—"


def test_format_avg_minutes_only():
    assert _format_avg(420) == "7мин"


def test_format_avg_hours_minutes():
    out = _format_avg(3 * 3600 + 25 * 60)
    # минуты при наличии часов не показываются; чисто «3ч»
    assert "ч" in out


# ---- Баг B: _allowed_scopes учитывает department_id ----


def test_allowed_scopes_admin_with_dept():
    assert _allowed_scopes(_u(UserRole.ADMIN.value, dept=5)) == [
        "user",
        "department",
        "all",
    ]


def test_allowed_scopes_admin_without_dept_excludes_department():
    """Корневой фикс: admin без отдела не должен иметь кнопку «Мой отдел»."""
    assert _allowed_scopes(_u(UserRole.ADMIN.value, dept=None)) == ["user", "all"]


def test_allowed_scopes_lead_with_dept():
    assert _allowed_scopes(_u(UserRole.LEAD.value, dept=3)) == [
        "user",
        "department",
    ]


def test_allowed_scopes_lead_without_dept():
    assert _allowed_scopes(_u(UserRole.LEAD.value, dept=None)) == ["user"]


def test_allowed_scopes_employee_empty():
    assert _allowed_scopes(_u(UserRole.EMPLOYEE.value, dept=1)) == []


# ---- Баг C: _safe_scope нормализует невалидный state ----


def test_safe_scope_valid_kept():
    user = _u(UserRole.ADMIN.value, dept=None)
    assert _safe_scope(user, "all") == "all"
    assert _safe_scope(user, "user") == "user"


def test_safe_scope_invalid_department_admin_no_dept_falls_to_all():
    """Сценарий бага: в FSM застрял scope=department, у admin нет dept."""
    user = _u(UserRole.ADMIN.value, dept=None)
    assert _safe_scope(user, "department") == "all"


def test_safe_scope_invalid_for_lead_no_dept_falls_to_user():
    user = _u(UserRole.LEAD.value, dept=None)
    assert _safe_scope(user, "department") == "user"


def test_safe_scope_none_falls_back():
    user = _u(UserRole.ADMIN.value, dept=10)
    assert _safe_scope(user, None) == "all"
