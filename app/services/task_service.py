"""
Оркестратор создания задачи (transactional outbox pattern).

С Этапа А «Бот без чата»: одна транзакция БД =
  INSERT tasks
  INSERT task_files
  INSERT task_history(created)
  INSERT task_outbox(broadcast)
  COMMIT

После коммита — синхронный `OutboxService.process_now` (action=broadcast):
worker рассылает карточку каждому активному approved-участнику направления
в личку, регистрирует доставки в task_broadcasts, постановщику в личку
шлёт сводку «получили N, не доставлено M». Фоновый scheduler-job каждую
минуту дожмёт ретраи.

Старые post_dept/post_lead записи в outbox (для задач до Этапа А)
остаются обрабатываемыми ради совместимости — см. outbox_service._post_dept
и _post_lead. Новые задачи в эти ветки не идут.
"""

import asyncio
from datetime import datetime
from typing import Any, Sequence

from aiogram import Bot
from loguru import logger

from app.db.base import async_session_factory
from app.db.enums import HistoryEventType, TaskPriority
from app.db.models import Task, User
from app.db.repositories.departments import DepartmentsRepository
from app.db.repositories.history import HistoryRepository
from app.db.repositories.outbox import OutboxRepository
from app.db.repositories.tasks import TasksRepository
from app.scheduler.bootstrap import schedule_reminders_for_task
from app.scheduler.runtime import get_scheduler
from app.services.outbox_service import OutboxService

# Сколько максимально ждём синхронной публикации в `cb_send`. Если TG
# завис (DNS, TCP) — не держим юзера дольше этого, отдаём управление и
# доверяем фоновому outbox-job'у дожать публикацию.
SYNC_PUBLISH_TIMEOUT_S = 15


# _post_files перенесён в app/services/outbox_service.py — публикация и
# карточек, и приложенных файлов идёт через outbox-worker.


class TaskService:
    @staticmethod
    async def create_task(
        *,
        bot: Bot,
        creator: User,
        department_id: int,
        title: str,
        priority: TaskPriority,
        deadline_utc: datetime,
        description: str | None,
        files: Sequence[dict[str, Any]],
    ) -> Task:
        """Создаёт задачу + outbox-задания на публикацию.

        Возвращает Task всегда (если БД работает) — Telegram-сбои больше
        не пробрасываются наружу. Публикацией занимается OutboxService:
        синхронно в `process_now` сразу после COMMIT (юзер видит карточку
        мгновенно при здоровом TG) и фоновым job'ом каждую минуту
        (для retry при сбоях).
        """
        async with async_session_factory() as session:
            async with session.begin():
                dept = await DepartmentsRepository.get_by_id(session, department_id)
                if dept is None:
                    raise ValueError(f"Department id={department_id} not found")

                task = await TasksRepository.create(
                    session,
                    title=title,
                    description=description,
                    priority=priority,
                    deadline=deadline_utc,
                    creator_id=creator.id,
                    department_id=department_id,
                )
                if files:
                    await TasksRepository.add_files(session, task.id, files)
                await HistoryRepository.log(
                    session,
                    task_id=task.id,
                    user_id=creator.id,
                    event_type=HistoryEventType.CREATED,
                )

                # Этап А «Бот без чата»: вместо post_dept/post_lead в супергруппу
                # ставим единую запись broadcast — outbox-worker внутри цикла
                # шлёт карточку в личку каждому активному участнику направления
                # и регистрирует доставку в task_broadcasts.
                await OutboxRepository.add(session, task.id, "broadcast")
                # Этап Д: зеркало в закрытый топик «Руководство» — даёт
                # админам единый ленту-архив всех созданных задач без
                # дублирования в Mini App. На жизненном цикле сообщение
                # этого зеркала редактируется action'ом update_lead.
                await OutboxRepository.add(session, task.id, "post_lead")

        # COMMIT прошёл. Задача гарантировано в БД. Дальше — попытка
        # синхронной публикации с жёстким таймаутом, чтобы зависший
        # Telegram не держал юзера в FSM-cb_send'е. Любые TG-сбои тут
        # не откатывают коммит и не пробрасываются — Outbox дожмёт сам.
        try:
            await asyncio.wait_for(
                OutboxService.process_now(bot, task.id),
                timeout=SYNC_PUBLISH_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "task #{}: sync publish timed out (>{}s) — outbox will retry",
                task.id,
                SYNC_PUBLISH_TIMEOUT_S,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "task #{}: sync publish failed, will retry in background: {}",
                task.id,
                exc,
            )

        try:
            scheduled = schedule_reminders_for_task(
                get_scheduler(),
                task_id=task.id,
                deadline_utc=task.deadline,
            )
            logger.debug(
                "task #{}: scheduled reminders={}",
                task.id,
                [rt.value for rt in scheduled],
            )
        except RuntimeError as exc:
            logger.warning("schedule_reminders_for_task skipped: {}", exc)

        return task
