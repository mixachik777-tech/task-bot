"""
APScheduler job-функции:

- send_reminder(task_id, reminder_type) — DateTrigger, расставляется
  per-task через schedule_reminders_for_task.
- check_overdue() — IntervalTrigger 1ч, сканит просрочки и уведомляет
  leads dept'а + admins (Redis dedup, TTL 24h).
- purge_completed_messages() — IntervalTrigger 6ч, удаляет dept-карточки
  done-задач старше 24ч от completed_at (зеркало в «Руководстве» НЕ трогаем).

Все job'ы открывают свою сессию через async_session_factory; bot и
scheduler берутся через global accessors из app/bot/runtime и
app/scheduler/runtime.

Race-policy для send_reminder: lock → проверка → UPDATE флаг → commit.
ПОСЛЕ commit'а DM. Если DM провалится — WARNING, флаг уже стоит,
повтор не делаем (см. DECISIONS.md → Этап 6).
"""

import html
from datetime import datetime
from typing import Sequence

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from loguru import logger
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.bot.runtime import get_bot
from app.bot.utils.render import (
    PRIORITY_EMOJI,
    PRIORITY_NAME,
)
from app.bot.utils.tg import build_topic_message_link
from app.bot.utils.time import format_dt_local, now_utc
from app.db.base import async_session_factory
from app.db.enums import REMINDER_CONFIG, ReminderType, TaskStatus, UserRole
from app.db.models import Task, User
from app.db.repositories.tasks import TasksRepository
from app.db.repositories.users import UsersRepository
from app.scheduler.notifications import mark_overdue_notified, notify_overdue
from app.services.overdue_topic import post_overdue_card  # noqa: F401 — используется в check_overdue


# ──────────────────────────────────────────────────────────────────────
# Helpers (тестируемые без БД)
# ──────────────────────────────────────────────────────────────────────


def make_reminder_job_id(task_id: int, reminder_type: ReminderType) -> str:
    return f"reminder:{task_id}:{reminder_type.value}"


def compute_remind_time(deadline_utc: datetime, reminder_type: ReminderType) -> datetime:
    _, threshold = REMINDER_CONFIG[reminder_type]
    return deadline_utc - threshold


def should_skip_send_reminder(*, status: str, flag_already_set: bool) -> bool:
    """True, если send_reminder должен ничего не делать."""
    if flag_already_set:
        return True
    if status not in {TaskStatus.NEW.value, TaskStatus.IN_PROGRESS.value}:
        return True
    return False


def render_reminder_text(task: Task, reminder_type: ReminderType) -> str:
    label = {
        ReminderType.D2: "за 2 дня",
        ReminderType.D1: "за 1 день",
        ReminderType.H2: "за 2 часа",
    }[reminder_type]
    return (
        f"⏰ Напоминание ({label}) о задаче #{task.display_number}\n"
        f"📌 {html.escape(task.title or '')}\n"
        f"{PRIORITY_EMOJI[task.priority]} Важность: {PRIORITY_NAME[task.priority]}\n"
        f"⏰ Срок: {format_dt_local(task.deadline)}"
    )


def _assignee_handle(assignee: User | None, status: str) -> str:
    """Кто отвечает за просроченную задачу. None → «не назначена» (status=new)."""
    if assignee is None:
        return "не назначена (статус «Новая»)"
    if assignee.tg_username:
        return f"@{html.escape(assignee.tg_username)}"
    name = assignee.full_name or f"id={assignee.tg_user_id}"
    return html.escape(name)


def render_overdue_text(
    task: Task,
    overdue_seconds: int,
    *,
    task_chat_id: int | None = None,
    lead_topic_id: int | None = None,
) -> str:
    """Текст overdue-уведомления. ParseMode=HTML.

    Раньше ссылка строилась через `task.dept_chat_id` + `department.topic_id`,
    но после Этапа А (broadcast в личку) `dept_chat_id` = личный chat
    пользователя-взявшего, и линк ведёт в его личку — недоступную ни лидам,
    ни админам, ни постановщику. Теперь ссылка строится через зеркало в
    «Руководстве» (task_chat_id + LEADERSHIP_TOPIC_ID + task.arch_message_id) —
    туда есть доступ у всех админов/лидов, кто видит супергруппу редакции.
    """
    hours_overdue = overdue_seconds // 3600
    link = None
    if task_chat_id and lead_topic_id and task.arch_message_id:
        link = build_topic_message_link(task_chat_id, lead_topic_id, task.arch_message_id)
    lines = [
        f"🚨 Просрочена задача #{task.display_number}",
        f"📌 {html.escape(task.title)}",
        f"{PRIORITY_EMOJI[task.priority]} Важность: {PRIORITY_NAME[task.priority]}",
        f"👤 Исполнитель: {_assignee_handle(task.assignee, task.status)}",
        f"⏰ Был срок: {format_dt_local(task.deadline)} (просрочено {hours_overdue}ч)",
    ]
    if link:
        lines.append(f'🔗 <a href="{link}">Открыть в Руководстве</a>')
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────
# Job: send_reminder
# ──────────────────────────────────────────────────────────────────────


async def send_reminder(task_id: int, reminder_type_value: str) -> None:
    """
    Один-shot DateTrigger job. APScheduler сериализует args, поэтому
    reminder_type приходит строкой; конвертируем в Enum здесь.
    """
    try:
        reminder_type = ReminderType(reminder_type_value)
    except ValueError:
        logger.error("send_reminder: unknown reminder_type={}", reminder_type_value)
        return

    flag_attr, _ = REMINDER_CONFIG[reminder_type]

    recipient_tg_id: int | None = None
    notification_text: str | None = None

    async with async_session_factory() as session:
        async with session.begin():
            task = await TasksRepository.lock_for_update(session, task_id)
            if task is None:
                logger.warning("send_reminder: task #{} not found", task_id)
                return

            flag_already_set = bool(getattr(task, flag_attr))
            if should_skip_send_reminder(status=task.status, flag_already_set=flag_already_set):
                logger.debug(
                    "send_reminder skip: task #{} status={} {}={}",
                    task.id,
                    task.status,
                    flag_attr,
                    flag_already_set,
                )
                return

            # Recipient: in_progress → assignee, new → creator (см. PROJECT_PLAN.md)
            recipient_user_id: int | None
            if task.status == TaskStatus.IN_PROGRESS.value:
                recipient_user_id = task.assignee_id
            else:
                recipient_user_id = task.creator_id

            if recipient_user_id is None:
                logger.warning(
                    "send_reminder: task #{} status={} но recipient_user_id=None",
                    task.id,
                    task.status,
                )
                return

            recipient = await UsersRepository.get_by_id(session, recipient_user_id)
            if recipient is None or recipient.tg_user_id is None:
                logger.warning("send_reminder: recipient user_id={} не найден", recipient_user_id)
                return

            # SET flag → commit; DM делаем после
            setattr(task, flag_attr, True)
            recipient_tg_id = recipient.tg_user_id
            notification_text = render_reminder_text(task, reminder_type)

    # После commit — best-effort DM
    if recipient_tg_id and notification_text:
        bot = get_bot()
        try:
            await bot.send_message(chat_id=recipient_tg_id, text=notification_text)
            logger.info("reminder {} sent for task #{}", reminder_type.value, task_id)
        except (TelegramBadRequest, TelegramForbiddenError) as exc:
            logger.warning(
                "reminder DM failed for task #{} → {}: {}",
                task_id,
                recipient_tg_id,
                exc,
            )


# ──────────────────────────────────────────────────────────────────────
# Job: check_overdue
# ──────────────────────────────────────────────────────────────────────


async def _list_overdue_tasks(session: AsyncSession, threshold_dt: datetime) -> Sequence[Task]:
    """
    Грузим assignee + department сразу: render_overdue_text использует имена
    и topic_id, а это async-сессия — без selectinload произошёл бы
    MissingGreenlet при lazy-load.
    """
    result = await session.execute(
        select(Task)
        .options(
            selectinload(Task.assignee),
            selectinload(Task.department),
        )
        .where(
            Task.status.in_([TaskStatus.NEW.value, TaskStatus.IN_PROGRESS.value]),
            Task.deadline < threshold_dt,
        )
    )
    return result.scalars().all()


async def _list_recipients_for_dept(session: AsyncSession, department_id: int) -> list[User]:
    """Leads данного отдела + ВСЕ admin (вне зависимости от department_id)."""
    result = await session.execute(
        select(User).where(
            User.is_active.is_(True),
            (
                ((User.role == UserRole.LEAD.value) & (User.department_id == department_id))
                | (User.role == UserRole.ADMIN.value)
            ),
        )
    )
    return list(result.scalars().all())


async def check_overdue() -> None:
    """IntervalTrigger 1ч: сканирует overdue-задачи, шлёт DM с Redis-дедупом 24ч."""
    bot = get_bot()
    threshold_dt = now_utc()  # просрочка любая
    notified_count = 0
    skipped_count = 0

    # настройки для ссылки в Руководстве — читаем один раз на весь скан
    from app.db.repositories.app_settings import (
        KEY_LEADERSHIP_TOPIC_ID,
        KEY_TASK_CHAT_ID,
        AppSettingsRepository,
    )

    async with async_session_factory() as session:
        task_chat_id = await AppSettingsRepository.get_int(session, KEY_TASK_CHAT_ID)
        lead_topic_id = await AppSettingsRepository.get_int(session, KEY_LEADERSHIP_TOPIC_ID)
        # SELECT-only, без транзакционных UPDATE — read-committed достаточно
        tasks = await _list_overdue_tasks(session, threshold_dt)

        for task in tasks:
            # Карточка в overdue-топике — отдельная дедупликация: пост
            # только если ещё не публиковалась (overdue_message_id NULL).
            # Делаем ДО DM-дедупа, иначе крэш бота между notify и post
            # навсегда оставит задачу без отметки в топике.
            try:
                await post_overdue_card(bot, task.id)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "overdue: post_overdue_card failed for task #{}: {}",
                    task.id,
                    exc,
                )

            should_notify = await mark_overdue_notified(task.id)
            if not should_notify:
                skipped_count += 1
                continue

            recipients = await _list_recipients_for_dept(session, task.department_id)
            if not recipients:
                logger.debug("overdue: task #{} нет получателей (нет leads/admin)", task.id)
                continue

            overdue_seconds = int((now_utc() - task.deadline).total_seconds())
            text = render_overdue_text(
                task,
                overdue_seconds,
                task_chat_id=task_chat_id,
                lead_topic_id=lead_topic_id,
            )
            delivered = await notify_overdue(bot, task.id, recipients, text)
            if delivered:
                notified_count += 1
            logger.info(
                "overdue task #{} → {}/{} delivered",
                task.id,
                delivered,
                len(recipients),
            )

    logger.info(
        "check_overdue: scanned={}, notified={}, deduped={}",
        len(tasks),
        notified_count,
        skipped_count,
    )


# ──────────────────────────────────────────────────────────────────────
# Job: purge_test_tasks — стирает "Тестовая/Тест/тест" задачи старше 30 мин
# ──────────────────────────────────────────────────────────────────────


# Регэксп тестовости заголовка. ILIKE-friendly.
# Совпадает: "тест", "Тест", "тестовая", "Тестовое", "test 123", "TEST".
# НЕ совпадает: "тестировщик" (не в начале слова), "латест" — потому что нужен
# word-boundary через ~ '(^|\W)(тест|test)' для регистронезависимого поиска.
TEST_TASK_REGEX = r"(^|\W)(тест|test)"
TEST_TASK_TTL_MINUTES = 30


async def purge_test_tasks() -> None:
    """IntervalTrigger 5мин: стирает все «тестовые» задачи старше 30 минут.

    Пользователь устаёт от тестовых задач, которые висят в списках и
    путают (см. 2026-05-29). Триггер сам:
      1) находит задачи, у которых title начинается с «тест/Тест/test»,
      2) старше TEST_TASK_TTL_MINUTES,
      3) каскадно удаляет: TaskOutbox, TaskBroadcast, TaskFile, TaskHistory, Task,
      4) и пытается удалить связанные сообщения из чатов
         (dept, lead-зеркало, overdue-топик).

    Не трогает задачи моложе 30 минут — даём окно создать и проверить.
    """
    from datetime import timedelta
    from sqlalchemy import delete as sa_delete, text as sa_text

    from app.db.models import TaskBroadcast, TaskFile, TaskHistory, TaskOutbox
    from app.db.repositories.app_settings import KEY_TASK_CHAT_ID, AppSettingsRepository

    bot = get_bot()
    cutoff = now_utc() - timedelta(minutes=TEST_TASK_TTL_MINUTES)

    async with async_session_factory() as session:
        rows = await session.execute(
            sa_text(
                """
                SELECT id, display_number, dept_chat_id, dept_message_id,
                       arch_message_id, overdue_message_id, title
                FROM tasks
                WHERE created_at < :cutoff AND title ~* :rx
                """
            ),
            {"cutoff": cutoff, "rx": TEST_TASK_REGEX},
        )
        candidates = [dict(r._mapping) for r in rows]
        task_chat_id = await AppSettingsRepository.get_int(session, KEY_TASK_CHAT_ID) or 0

    if not candidates:
        logger.debug("purge_test_tasks: ничего удалять не нужно")
        return

    deleted_ids: list[int] = []
    for c in candidates:
        # 1) удаляем сопутствующие TG-сообщения (best-effort)
        for chat, msg in (
            (c["dept_chat_id"], c["dept_message_id"]),
            (task_chat_id, c["arch_message_id"]),
            (task_chat_id, c["overdue_message_id"]),
        ):
            if chat and msg:
                try:
                    await bot.delete_message(chat_id=chat, message_id=int(msg))
                except Exception as exc:  # noqa: BLE001
                    logger.debug(
                        "purge_test_tasks: TG delete failed for task #{} msg={}: {}",
                        c["id"],
                        msg,
                        exc,
                    )
        # 2) каскадное удаление в БД
        try:
            async with async_session_factory() as s2:
                async with s2.begin():
                    await s2.execute(sa_delete(TaskOutbox).where(TaskOutbox.task_id == c["id"]))
                    await s2.execute(
                        sa_delete(TaskBroadcast).where(TaskBroadcast.task_id == c["id"])
                    )
                    await s2.execute(sa_delete(TaskFile).where(TaskFile.task_id == c["id"]))
                    await s2.execute(sa_delete(TaskHistory).where(TaskHistory.task_id == c["id"]))
                    await s2.execute(sa_delete(Task).where(Task.id == c["id"]))
            deleted_ids.append(c["id"])
        except Exception as exc:  # noqa: BLE001
            logger.warning("purge_test_tasks: DB delete failed for task #{}: {}", c["id"], exc)

    logger.info(
        "purge_test_tasks: scanned={}, deleted={} (ids={})",
        len(candidates),
        len(deleted_ids),
        deleted_ids,
    )


# ──────────────────────────────────────────────────────────────────────
# Job: purge_completed_messages
# ──────────────────────────────────────────────────────────────────────


async def _list_purgeable(session: AsyncSession, cutoff_dt: datetime) -> Sequence[Task]:
    result = await session.execute(
        select(Task).where(
            Task.status == TaskStatus.DONE.value,
            Task.completed_at < cutoff_dt,
            Task.is_message_purged.is_(False),
            Task.dept_chat_id.is_not(None),
            Task.dept_message_id.is_not(None),
        )
    )
    return result.scalars().all()


async def purge_completed_messages() -> None:
    """
    IntervalTrigger 6ч: удаляет dept-карточки done-задач старше 24ч.

    Архитектура — две отдельные сессии:
      1) SELECT кандидатов в простые dict'ы → close session
      2) bot.delete_message по списку (БЕЗ открытой транзакции БД)
      3) Открыть новую сессию → один bulk UPDATE по ids успешных

    Это исключает баг "transaction already begun" при попытке
    nested begin() поверх auto-begin SELECT'а в одной сессии.
    """
    from datetime import timedelta

    bot = get_bot()
    cutoff = now_utc() - timedelta(hours=24)
    purged = 0
    failed = 0

    # Сессия 1: SELECT и материализация в dict'ы
    async with async_session_factory() as session:
        async with session.begin():
            tasks = await _list_purgeable(session, cutoff)
            candidates = [
                {
                    "id": t.id,
                    "dept_chat_id": t.dept_chat_id,
                    "dept_message_id": t.dept_message_id,
                }
                for t in tasks
            ]

    # Между сессиями: TG-вызовы — без открытой транзакции БД
    ids_to_mark: list[int] = []
    for c in candidates:
        try:
            await bot.delete_message(chat_id=c["dept_chat_id"], message_id=c["dept_message_id"])
        except TelegramBadRequest as exc:
            if "not found" in str(exc).lower():
                logger.debug("purge: task #{} message already gone", c["id"])
            else:
                logger.warning("purge: task #{} BadRequest: {}", c["id"], exc)
        except TelegramForbiddenError as exc:
            logger.warning("purge: task #{} Forbidden: {}", c["id"], exc)
            failed += 1
            continue
        ids_to_mark.append(c["id"])
        purged += 1

    # Сессия 2: один bulk UPDATE по списку успешных id
    if ids_to_mark:
        async with async_session_factory() as session:
            async with session.begin():
                await session.execute(
                    update(Task).where(Task.id.in_(ids_to_mark)).values(is_message_purged=True)
                )

    logger.info(
        "purge_completed: scanned={}, purged={}, failed={}",
        len(candidates),
        purged,
        failed,
    )


# ──────────────────────────────────────────────────────────────────────
# Job: process_outbox — retry публикации задач в Telegram (Fix #2 outbox)
# ──────────────────────────────────────────────────────────────────────


async def process_outbox() -> None:
    """IntervalTrigger 1мин: дожимает невыполненные публикации в Telegram.

    Защита от «зависших в БД» задач: даже если бот упал во время
    создания задачи, при следующем запуске scheduler-job увидит
    pending-запись в task_outbox и попробует опубликовать ещё раз.
    Эту же логику зовёт TaskService синхронно сразу после COMMIT'а
    через OutboxService.process_now — для мгновенной публикации
    при здоровом Telegram.
    """
    from app.services.outbox_service import OutboxService

    bot = get_bot()
    try:
        processed = await OutboxService.process_pending(bot, limit=50)
        if processed > 0:
            logger.info("outbox: processed {} pending rows", processed)
    except Exception as exc:  # noqa: BLE001
        logger.exception("outbox job crashed: {}", exc)
