"""
Bootstrap APScheduler при старте бота:

1. Регистрирует interval-job'ы: check_overdue (1ч), purge_completed (6ч).
2. Сканит активные задачи (status IN (new, in_progress)) и расставляет
   DateTrigger-job'ы для всех ЕЩЁ-НЕ-отправленных reminder'ов
   (флаг reminder_*_sent=False) с timepoint'ом В БУДУЩЕМ.
3. Подвешивает EVENT_JOB_ERROR listener для логирования упавших job'ов.
"""

from datetime import datetime

from apscheduler.events import EVENT_JOB_ERROR, JobExecutionEvent
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger
from loguru import logger
from sqlalchemy import select

from app.bot.utils.time import now_utc
from app.db.base import async_session_factory
from app.db.enums import REMINDER_CONFIG, ReminderType, TaskStatus
from app.db.models import Task
from app.scheduler.jobs import (
    check_overdue,
    compute_remind_time,
    make_reminder_job_id,
    process_outbox,
    purge_completed_messages,
    purge_test_tasks,  # noqa: F401 — используется в bootstrap_scheduler ниже
    send_reminder,
)

JOB_OVERDUE = "overdue_check"
JOB_PURGE = "purge_completed"
JOB_OUTBOX = "process_outbox"
JOB_TEST_PURGE = "purge_test_tasks"

REMINDER_FLAG_BY_TYPE: dict[ReminderType, str] = {rt: REMINDER_CONFIG[rt][0] for rt in ReminderType}


def _on_job_error(event: JobExecutionEvent) -> None:
    logger.error("APScheduler job '{}' raised: {}", event.job_id, event.exception)


def schedule_reminders_for_task(
    scheduler: AsyncIOScheduler,
    *,
    task_id: int,
    deadline_utc: datetime,
    skip: set[ReminderType] | None = None,
) -> list[ReminderType]:
    """
    Планирует DateTrigger-job'ы для каждого reminder-типа, у которого
    timepoint > now. Возвращает список фактически запланированных типов.

    skip — типы, которые уже отправлены (флаг True в БД); их пропускаем.
    """
    skip = skip or set()
    scheduled: list[ReminderType] = []
    for reminder_type in ReminderType:
        if reminder_type in skip:
            continue
        run_time = compute_remind_time(deadline_utc, reminder_type)
        if run_time <= now_utc():
            continue
        scheduler.add_job(
            send_reminder,
            trigger=DateTrigger(run_date=run_time),
            id=make_reminder_job_id(task_id, reminder_type),
            args=[task_id, reminder_type.value],
            replace_existing=True,
            misfire_grace_time=300,
        )
        scheduled.append(reminder_type)
    return scheduled


def remove_reminders_for_task(scheduler: AsyncIOScheduler, task_id: int) -> None:
    """Снимает все 3 reminder-job'а; не падает, если их нет."""
    for reminder_type in ReminderType:
        job_id = make_reminder_job_id(task_id, reminder_type)
        try:
            scheduler.remove_job(job_id)
        except Exception as exc:  # noqa: BLE001 — JobLookupError и др.
            logger.debug("remove_job '{}' пропущен: {}", job_id, exc)


async def bootstrap_scheduler(scheduler: AsyncIOScheduler) -> None:
    """
    Расставляет interval'ы + вычисляет per-task reminder'ы для всех
    активных задач. Вызывается ОДИН раз перед scheduler.start().
    """
    scheduler.add_listener(_on_job_error, EVENT_JOB_ERROR)

    scheduler.add_job(
        check_overdue,
        trigger=IntervalTrigger(hours=1),
        id=JOB_OVERDUE,
        replace_existing=True,
        coalesce=True,
        misfire_grace_time=300,
    )
    scheduler.add_job(
        purge_completed_messages,
        trigger=IntervalTrigger(hours=6),
        id=JOB_PURGE,
        replace_existing=True,
        coalesce=True,
        misfire_grace_time=300,
    )
    scheduler.add_job(
        process_outbox,
        trigger=IntervalTrigger(minutes=1),
        id=JOB_OUTBOX,
        replace_existing=True,
        coalesce=True,
        misfire_grace_time=120,
    )
    scheduler.add_job(
        purge_test_tasks,
        trigger=IntervalTrigger(minutes=5),
        id=JOB_TEST_PURGE,
        replace_existing=True,
        coalesce=True,
        misfire_grace_time=300,
    )

    total_tasks = 0
    total_jobs = 0
    async with async_session_factory() as session:
        result = await session.execute(
            select(Task).where(
                Task.status.in_([TaskStatus.NEW.value, TaskStatus.IN_PROGRESS.value])
            )
        )
        for task in result.scalars():
            total_tasks += 1
            already_sent: set[ReminderType] = {
                rt for rt, attr in REMINDER_FLAG_BY_TYPE.items() if bool(getattr(task, attr))
            }
            scheduled = schedule_reminders_for_task(
                scheduler,
                task_id=task.id,
                deadline_utc=task.deadline,
                skip=already_sent,
            )
            total_jobs += len(scheduled)

    logger.info(
        "scheduler bootstrap: {} active tasks → {} reminder jobs scheduled",
        total_tasks,
        total_jobs,
    )
