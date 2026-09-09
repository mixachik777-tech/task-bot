"""
Юнит-тесты помощников APScheduler-слоя:

- make_reminder_job_id — стабильный формат
- compute_remind_time — корректное вычитание timedelta
- should_skip_send_reminder — матрица скипа
- schedule_reminders_for_task — пропускает прошедшие timepoint'ы и уже-sent
- remove_reminders_for_task — не падает на отсутствующих job'ах
- render_reminder_text / render_overdue_text — содержат ключевые поля
"""

from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.bot.utils.tg import build_topic_message_link
from app.db.enums import REMINDER_CONFIG, ReminderType, TaskStatus
from app.db.models import Task, User
from app.scheduler.bootstrap import (
    remove_reminders_for_task,
    schedule_reminders_for_task,
)
from app.scheduler.jobs import (
    compute_remind_time,
    make_reminder_job_id,
    render_overdue_text,
    render_reminder_text,
    should_skip_send_reminder,
)


def _task_stub(*, deadline: datetime, status: str = TaskStatus.NEW.value) -> Task:
    return Task(
        id=42,
        display_number=42,
        title="Сверстать обложку",
        description=None,
        priority="high",
        status=status,
        deadline=deadline,
        creator_id=1,
        department_id=1,
    )


# ──────────────────────────────────────────────────────────────────────
# make_reminder_job_id
# ──────────────────────────────────────────────────────────────────────


def test_make_reminder_job_id_format():
    assert make_reminder_job_id(7, ReminderType.H2) == "reminder:7:h2"
    assert make_reminder_job_id(42, ReminderType.D1) == "reminder:42:d1"
    assert make_reminder_job_id(100, ReminderType.D2) == "reminder:100:d2"


def test_make_reminder_job_id_distinct_per_type():
    ids = {make_reminder_job_id(1, rt) for rt in ReminderType}
    assert len(ids) == 3


# ──────────────────────────────────────────────────────────────────────
# compute_remind_time
# ──────────────────────────────────────────────────────────────────────


def test_compute_remind_time_h2():
    deadline = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    assert compute_remind_time(deadline, ReminderType.H2) == datetime(
        2026, 6, 1, 10, 0, tzinfo=timezone.utc
    )


def test_compute_remind_time_d1():
    deadline = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    assert compute_remind_time(deadline, ReminderType.D1) == datetime(
        2026, 5, 31, 12, 0, tzinfo=timezone.utc
    )


def test_compute_remind_time_d2():
    deadline = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    assert compute_remind_time(deadline, ReminderType.D2) == datetime(
        2026, 5, 30, 12, 0, tzinfo=timezone.utc
    )


def test_compute_remind_time_uses_config_threshold():
    deadline = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    for rt, (_, threshold) in REMINDER_CONFIG.items():
        assert compute_remind_time(deadline, rt) == deadline - threshold


# ──────────────────────────────────────────────────────────────────────
# should_skip_send_reminder
# ──────────────────────────────────────────────────────────────────────


def test_should_skip_when_flag_already_set():
    assert (
        should_skip_send_reminder(
            status=TaskStatus.IN_PROGRESS.value,
            flag_already_set=True,
        )
        is True
    )


def test_should_skip_when_status_done():
    assert (
        should_skip_send_reminder(
            status=TaskStatus.DONE.value,
            flag_already_set=False,
        )
        is True
    )


def test_should_skip_when_status_cancelled():
    assert (
        should_skip_send_reminder(
            status=TaskStatus.CANCELLED.value,
            flag_already_set=False,
        )
        is True
    )


def test_should_NOT_skip_for_new_unset_flag():
    assert (
        should_skip_send_reminder(
            status=TaskStatus.NEW.value,
            flag_already_set=False,
        )
        is False
    )


def test_should_NOT_skip_for_in_progress_unset_flag():
    assert (
        should_skip_send_reminder(
            status=TaskStatus.IN_PROGRESS.value,
            flag_already_set=False,
        )
        is False
    )


# ──────────────────────────────────────────────────────────────────────
# schedule_reminders_for_task / remove_reminders_for_task
# ──────────────────────────────────────────────────────────────────────


def _real_scheduler() -> AsyncIOScheduler:
    s = AsyncIOScheduler(timezone=timezone.utc)
    return s  # не вызываем .start() — тесты только манипулируют jobstore


def test_schedule_reminders_skips_past_timepoints():
    s = _real_scheduler()
    # deadline через 1 час → d2 (-2д) и d1 (-1д) в прошлом, h2 (-2ч)
    # тоже в прошлом (-1ч). Все три скипаются.
    deadline = datetime.now(timezone.utc) + timedelta(hours=1)
    scheduled = schedule_reminders_for_task(s, task_id=1, deadline_utc=deadline)
    assert scheduled == []
    assert s.get_job("reminder:1:h2") is None


def test_schedule_reminders_schedules_only_future():
    s = _real_scheduler()
    # deadline через 5 дней → все три (h2, d1, d2) в будущем
    deadline = datetime.now(timezone.utc) + timedelta(days=5)
    scheduled = schedule_reminders_for_task(s, task_id=2, deadline_utc=deadline)
    assert set(scheduled) == set(ReminderType)
    for rt in ReminderType:
        assert s.get_job(make_reminder_job_id(2, rt)) is not None


def test_schedule_reminders_respects_skip_set():
    s = _real_scheduler()
    deadline = datetime.now(timezone.utc) + timedelta(days=5)
    scheduled = schedule_reminders_for_task(
        s,
        task_id=3,
        deadline_utc=deadline,
        skip={ReminderType.D1, ReminderType.H2},
    )
    assert scheduled == [ReminderType.D2]
    assert s.get_job(make_reminder_job_id(3, ReminderType.D2)) is not None
    assert s.get_job(make_reminder_job_id(3, ReminderType.D1)) is None


def test_schedule_reminders_partial_when_some_in_past():
    s = _real_scheduler()
    # deadline через 25 часов → d1 (~через час) в будущем, d2 (-23ч) в прошлом,
    # h2 (через 23ч) в будущем
    deadline = datetime.now(timezone.utc) + timedelta(hours=25)
    scheduled = schedule_reminders_for_task(s, task_id=4, deadline_utc=deadline)
    assert ReminderType.H2 in scheduled
    assert ReminderType.D1 in scheduled
    assert ReminderType.D2 not in scheduled


def test_remove_reminders_does_not_raise_when_jobs_missing():
    s = _real_scheduler()
    # Нет ни одного job'а — функция должна не упасть
    remove_reminders_for_task(s, task_id=999)


def test_remove_reminders_drops_existing_jobs():
    s = _real_scheduler()
    deadline = datetime.now(timezone.utc) + timedelta(days=5)
    schedule_reminders_for_task(s, task_id=10, deadline_utc=deadline)
    assert s.get_job(make_reminder_job_id(10, ReminderType.H2)) is not None

    remove_reminders_for_task(s, task_id=10)
    for rt in ReminderType:
        assert s.get_job(make_reminder_job_id(10, rt)) is None


def test_remove_reminders_partial_already_gone():
    s = _real_scheduler()
    deadline = datetime.now(timezone.utc) + timedelta(days=5)
    schedule_reminders_for_task(s, task_id=11, deadline_utc=deadline)
    s.remove_job(make_reminder_job_id(11, ReminderType.H2))  # руками снимаем h2

    # Ожидаем: остальные снимутся, на h2 — try/except не падает
    remove_reminders_for_task(s, task_id=11)
    for rt in ReminderType:
        assert s.get_job(make_reminder_job_id(11, rt)) is None


# ──────────────────────────────────────────────────────────────────────
# render_reminder_text / render_overdue_text
# ──────────────────────────────────────────────────────────────────────


def test_render_reminder_text_has_label_and_id():
    task = _task_stub(deadline=datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc))
    out = render_reminder_text(task, ReminderType.D1)
    assert "за 1 день" in out
    assert "#42" in out
    assert "Сверстать обложку" in out


def test_render_reminder_text_h2_label():
    task = _task_stub(deadline=datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc))
    assert "за 2 часа" in render_reminder_text(task, ReminderType.H2)


def test_render_overdue_text_includes_hours():
    task = _task_stub(deadline=datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc))
    out = render_overdue_text(task, overdue_seconds=5 * 3600)  # 5 hours
    assert "просрочено 5ч" in out
    assert "#42" in out


def test_render_overdue_text_unassigned_shows_label():
    task = _task_stub(deadline=datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc))
    task.assignee = None  # status=new по дефолту в стабе
    out = render_overdue_text(task, overdue_seconds=3600)
    assert "Исполнитель:" in out
    assert "не назначена" in out


def test_render_overdue_text_with_assignee_shows_handle():
    task = _task_stub(
        deadline=datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc),
        status=TaskStatus.IN_PROGRESS.value,
    )
    task.assignee = User(
        id=2,
        tg_user_id=10,
        tg_username="ivanov",
        full_name="Иван",
        role="employee",
        is_active=True,
    )
    out = render_overdue_text(task, overdue_seconds=7200)
    assert "@ivanov" in out


def test_render_overdue_text_includes_lead_link():
    """С 2026-05-29 ссылка в overdue ведёт на зеркало в «Руководстве»,
    а не в личку взявшего задачу (dept_chat_id = личка после accept-flow,
    туда никто кроме исполнителя попасть не может)."""
    task = _task_stub(
        deadline=datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc),
        status=TaskStatus.IN_PROGRESS.value,
    )
    task.arch_message_id = 142
    task.assignee = User(
        id=2,
        tg_user_id=10,
        tg_username="petrov",
        full_name="Петр",
        role="employee",
        is_active=True,
    )
    out = render_overdue_text(
        task,
        overdue_seconds=3600,
        task_chat_id=-1003718945546,
        lead_topic_id=9,
    )
    assert "https://t.me/c/3718945546/9/142" in out
    assert "Открыть в Руководстве" in out


def test_render_overdue_text_no_link_when_no_arch_message():
    """Если задача никогда не была опубликована в Руководстве
    (`arch_message_id is None`) — ссылку не строим, не плодим битые url."""
    task = _task_stub(deadline=datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc))
    task.arch_message_id = None
    task.assignee = None
    out = render_overdue_text(
        task,
        overdue_seconds=3600,
        task_chat_id=-1003718945546,
        lead_topic_id=9,
    )
    assert "Открыть в Руководстве" not in out
    assert "Открыть в чате" not in out


def test_render_overdue_text_no_link_when_settings_missing():
    task = _task_stub(deadline=datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc))
    task.arch_message_id = 142
    task.assignee = None
    out = render_overdue_text(task, overdue_seconds=3600)
    assert "Открыть" not in out


# ──────────────────────────────────────────────────────────────────────
# build_topic_message_link
# ──────────────────────────────────────────────────────────────────────


def test_topic_link_supergroup_with_topic():
    assert build_topic_message_link(-1003718945546, 3, 555) == "https://t.me/c/3718945546/3/555"


def test_topic_link_supergroup_no_topic():
    assert build_topic_message_link(-1003718945546, None, 555) == "https://t.me/c/3718945546/555"


def test_topic_link_returns_none_on_missing_ids():
    assert build_topic_message_link(0, 1, 100) is None
    assert build_topic_message_link(-100123456, 1, 0) is None
