from datetime import datetime, timedelta, timezone

from app.bot.utils.render import (
    render_archive_row,
    render_preview,
    render_task_card,
    render_task_history,
)
from app.db.enums import TaskPriority, TaskStatus
from app.db.models import Task, TaskHistory, User


def _user(tg_id: int = 1, username: str | None = "ivanov", name: str = "Ivan") -> User:
    return User(
        id=10,
        tg_user_id=tg_id,
        tg_username=username,
        full_name=name,
        role="employee",
        is_active=True,
    )


def _task(*, status: str = TaskStatus.NEW.value, priority: str = TaskPriority.HIGH.value) -> Task:
    return Task(
        id=42,
        display_number=42,
        title="Сверстать обложку",
        description="3 варианта",
        priority=priority,
        deadline=datetime.now(timezone.utc) + timedelta(hours=5),
        status=status,
        creator_id=10,
        department_id=1,
    )


def test_render_task_card_new():
    out = render_task_card(_task(), creator=_user(), files_count=2)
    assert "Задача #42" in out
    assert "Сверстать обложку" in out
    assert "Высокая" in out
    assert "@ivanov" in out
    assert "Файлов: 2" in out
    assert "🆕 Новая" in out


def test_render_task_card_in_progress_with_assignee():
    task = _task(status=TaskStatus.IN_PROGRESS.value)
    out = render_task_card(
        task,
        creator=_user(name="Creator", username="creator_u"),
        assignee=_user(tg_id=2, username="petrov", name="Petr"),
    )
    assert "В работе" in out
    assert "@petrov" in out


def test_render_task_card_escapes_html():
    task = _task()
    task.title = "<script>alert(1)</script>"
    out = render_task_card(task, creator=_user())
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_render_preview_includes_all_fields():
    deadline = datetime(2026, 5, 12, 9, 0, tzinfo=timezone.utc)  # 12:00 MSK
    out = render_preview(
        title="My task",
        department_name="Корректоры",
        priority="medium",
        deadline_utc=deadline,
        description="Detail",
        files_count=1,
    )
    assert "My task" in out
    assert "Корректоры" in out
    assert "Средняя" in out
    assert "12.05.2026 12:00" in out
    assert "Detail" in out
    assert "Файлов: 1" in out


def test_render_preview_skips_optional_fields():
    deadline = datetime(2026, 5, 12, 9, 0, tzinfo=timezone.utc)
    out = render_preview(
        title="Bare",
        department_name="X",
        priority="low",
        deadline_utc=deadline,
        description=None,
        files_count=0,
    )
    assert "Описание" not in out
    assert "Файлов" not in out


def test_render_task_card_multiline_description():
    """Многострочное описание (дизайнерское ТЗ) — выводится с переносами."""
    task = _task()
    task.description = (
        "📌 Назначение: афиша\n"
        "📐 Размеры: 1920×1080\n"
        "📄 Форматы: JPG, PSD\n"
        "🎨 Референс: https://example.com/poster.jpg"
    )
    out = render_task_card(task, creator=_user())
    # каждая строка ТЗ должна быть отдельно, заголовок «📝 Описание:» — на своей строке
    lines = out.split("\n")
    assert "📝 Описание:" in lines
    assert "📌 Назначение: афиша" in lines
    assert "📐 Размеры: 1920×1080" in lines
    assert "📄 Форматы: JPG, PSD" in lines
    # ссылка экранирована, но осталась читаемой
    assert any("https://example.com/poster.jpg" in ln for ln in lines)


def test_render_preview_multiline_description():
    deadline = datetime(2026, 5, 12, 9, 0, tzinfo=timezone.utc)
    out = render_preview(
        title="Дизайн афиши",
        department_name="Дизайнеры",
        priority="high",
        deadline_utc=deadline,
        description=("📌 Назначение: афиша\n📐 Размеры: А3\n📄 Форматы: PDF"),
        files_count=0,
    )
    lines = out.split("\n")
    assert "📝 Описание:" in lines
    assert "📌 Назначение: афиша" in lines
    assert "📐 Размеры: А3" in lines


# ---- overdue индикатор ----


def _overdue_task(status: str) -> Task:
    return Task(
        id=99,
        display_number=99,
        title="Просроченная",
        description=None,
        priority=TaskPriority.HIGH.value,
        deadline=datetime.now(timezone.utc) - timedelta(hours=4),
        status=status,
        creator_id=10,
        department_id=1,
    )


def test_render_task_card_overdue_new():
    out = render_task_card(_overdue_task(TaskStatus.NEW.value), creator=_user())
    assert "🔴 Просрочена" in out
    assert "🆕 Новая" not in out


def test_render_task_card_overdue_in_progress_includes_assignee():
    task = _overdue_task(TaskStatus.IN_PROGRESS.value)
    assignee = _user(tg_id=2, username="petrov", name="Petr")
    out = render_task_card(task, creator=_user(), assignee=assignee)
    assert "🔴 Просрочена" in out
    assert "@petrov" in out
    assert "В работе" not in out  # overdue заменяет «В работе»


def test_render_task_card_not_overdue_when_done_with_past_deadline():
    """Завершённая задача с просроченным deadline всё равно показывает '✅ Выполнено'."""
    task = _overdue_task(TaskStatus.DONE.value)
    out = render_task_card(task, creator=_user())
    assert "🔴 Просрочена" not in out
    assert "✅ Выполнено" in out


# ---- render_task_history ----


def _history_event(
    *,
    event_type: str,
    user_id: int | None = None,
    payload: dict | None = None,
    minute: int = 0,
) -> TaskHistory:
    return TaskHistory(
        id=minute + 1,
        task_id=1,
        user_id=user_id,
        event_type=event_type,
        payload=payload,
        created_at=datetime(2026, 5, 17, 9, minute, tzinfo=timezone.utc),
    )


def test_render_task_history_empty():
    assert render_task_history([]) == "—"


def test_render_task_history_basic_events():
    u_creator = _user(tg_id=1, username="creator_u", name="Creator")
    u_creator.id = 100
    u_assignee = _user(tg_id=2, username="petrov", name="Petr")
    u_assignee.id = 200
    events = [
        _history_event(event_type="created", user_id=100, minute=0),
        _history_event(event_type="accepted", user_id=200, minute=10),
        _history_event(
            event_type="completed",
            user_id=200,
            payload={"result_comment": "всё сдал"},
            minute=20,
        ),
    ]
    out = render_task_history(events, users_by_id={100: u_creator, 200: u_assignee})
    lines = out.split("\n")
    assert any("🆕 Создана" in ln and "@creator_u" in ln for ln in lines)
    assert any("✋ Принята" in ln and "@petrov" in ln for ln in lines)
    assert any("✅ Завершена" in ln and "всё сдал" in ln for ln in lines)


def test_render_task_history_commented_with_role_and_text():
    u = _user(tg_id=3, username="lead_u", name="Lead")
    u.id = 300
    events = [
        _history_event(
            event_type="commented",
            user_id=300,
            payload={"text": "обновлю до пятницы", "from_user_role": "lead"},
            minute=5,
        )
    ]
    out = render_task_history(events, users_by_id={300: u})
    assert "💬 Комментарий" in out
    assert "(руководитель)" in out
    assert "«обновлю до пятницы»" in out


def test_render_task_history_cancelled_by_role():
    events = [
        _history_event(
            event_type="cancelled",
            user_id=None,
            payload={"by_role": "admin"},
            minute=15,
        )
    ]
    out = render_task_history(events)
    assert "❌ Отменена" in out
    assert "(админ)" in out


# ---- archive row ----


def test_render_archive_row_truncates_long_title():
    task = Task(
        id=777,
        display_number=777,
        title="Очень длинное название задачи которое не помещается в кнопку Telegram inline keyboard",
        description=None,
        priority=TaskPriority.LOW.value,
        deadline=datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc),
        status=TaskStatus.DONE.value,
        creator_id=1,
        department_id=1,
        completed_at=datetime(2026, 5, 17, 14, 30, tzinfo=timezone.utc),
        created_at=datetime(2026, 5, 17, 11, 0, tzinfo=timezone.utc),
    )
    out = render_archive_row(task)
    assert out.startswith("#777")
    assert "✅" in out
    assert "…" in out  # обрезано


def test_render_archive_row_uses_created_at_when_no_completed_at(monkeypatch):
    task = Task(
        id=12,
        display_number=12,
        title="Отменено сразу",
        description=None,
        priority=TaskPriority.LOW.value,
        deadline=datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc),
        status=TaskStatus.CANCELLED.value,
        creator_id=1,
        department_id=1,
        completed_at=None,
        created_at=datetime(2026, 5, 17, 9, 15, tzinfo=timezone.utc),
    )
    out = render_archive_row(task)
    assert "#12" in out
    assert "❌" in out


def test_render_task_history_includes_reassigned_label():
    """Событие REASSIGNED отображается как «🔄 Переназначена» с локализованным
    лейблом из EVENT_LABEL."""
    from datetime import datetime, timezone
    from app.bot.utils.render import EVENT_LABEL, render_task_history
    from app.db.models import TaskHistory

    ev = TaskHistory(
        id=1, task_id=1, user_id=None,
        event_type="reassigned",
        payload={"from_user_id": 1, "to_user_id": 2, "by_role": "assignee"},
        created_at=datetime(2026, 5, 29, 12, 0, tzinfo=timezone.utc),
    )
    out = render_task_history([ev])
    assert EVENT_LABEL["reassigned"] in out
