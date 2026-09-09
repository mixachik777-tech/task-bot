"""
Топик «Просроченные задачи» в супергруппе (Этап Д).

Когда задача переходит в просрочку (check_overdue job), её карточка
постится в специальный топик с пометкой «🔴 Просрочена» — оперативный
дашборд для админа/лидов. При закрытии задачи (approve / cancel /
done) сообщение удаляется — топик содержит только то, что висит сейчас.

ID топика хранится в app_settings под ключом overdue_topic_id. Команда
установки — /set_overdue_topic из самого топика (см. cabinet.py admin
section). Если ключ не задан — функции тихо выходят, это не ошибка.

Поле в БД: tasks.overdue_message_id (BigInteger, NULL). NULL означает
«в топике этой задачи нет». Идемпотентно: если уже есть message_id —
не публикуем повторно. При покидании просрочки — DELETE сообщения и
сброс колонки в NULL.
"""

from __future__ import annotations

from aiogram import Bot
from loguru import logger
from sqlalchemy import update

from app.bot.utils.render import render_task_card
from app.bot.utils.tg import send_with_retry
from app.db.base import async_session_factory
from app.db.models import Task
from app.db.repositories.app_settings import (
    KEY_LEADERSHIP_TOPIC_ID,
    KEY_OVERDUE_TOPIC_ID,
    KEY_TASK_CHAT_ID,
    AppSettingsRepository,
)
from app.db.repositories.tasks import TasksRepository


async def _get_overdue_destination() -> tuple[int, int] | None:
    """Возвращает (chat_id, topic_id) или None если не настроено."""
    async with async_session_factory() as session:
        chat_id = (await AppSettingsRepository.get_int(session, KEY_TASK_CHAT_ID)) or 0
        topic_id = (await AppSettingsRepository.get_int(session, KEY_OVERDUE_TOPIC_ID)) or 0
    if chat_id == 0 or topic_id == 0:
        return None
    return chat_id, topic_id


async def post_overdue_card(bot: Bot, task_id: int) -> None:
    """Публикует карточку просроченной задачи в overdue-топик.

    Идемпотентно — если у задачи уже есть overdue_message_id, повторно
    не публикует. Best-effort: при ошибке логирует и продолжает.
    """
    dest = await _get_overdue_destination()
    if dest is None:
        return
    chat_id, topic_id = dest

    # 1) READ-only сессия для рендера карточки + чтение lead_topic_id
    async with async_session_factory() as session:
        task = await TasksRepository.get_full(session, task_id)
        if task is None:
            return
        if task.overdue_message_id is not None:
            return  # уже в топике
        if task.status not in ("new", "in_progress", "awaiting_approval"):
            return  # закрыта — постить нечего
        lead_topic_id = await AppSettingsRepository.get_int(session, KEY_LEADERSHIP_TOPIC_ID)
        text = render_task_card(
            task,
            creator=task.creator,
            assignee=task.assignee,
            department=task.department,
            files_count=len(task.files),
            task_chat_id=chat_id,
            lead_topic_id=lead_topic_id,
        )

    # 2) TG-сообщение (вне сессии БД)
    try:
        msg = await send_with_retry(
            bot,
            chat_id=chat_id,
            text=text,
            message_thread_id=topic_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("post_overdue_card: task #{} send failed: {}", task_id, exc)
        return

    # 3) Отдельная WRITE-сессия для сохранения overdue_message_id
    async with async_session_factory() as session:
        async with session.begin():
            await session.execute(
                update(Task).where(Task.id == task_id).values(overdue_message_id=msg.message_id)
            )


async def purge_overdue_card(bot: Bot, task_id: int) -> None:
    """Удаляет сообщение задачи из overdue-топика при её закрытии.

    Вызывается из approve / cancel / done. Если overdue_message_id NULL —
    задача никогда не висела в топике, ничего не делаем.
    """
    dest = await _get_overdue_destination()
    if dest is None:
        return
    chat_id, _ = dest

    async with async_session_factory() as session:
        task = await TasksRepository.get_by_id(session, task_id)
        if task is None or task.overdue_message_id is None:
            return
        msg_id = task.overdue_message_id

    try:
        await bot.delete_message(chat_id=chat_id, message_id=msg_id)
    except Exception as exc:  # noqa: BLE001
        # Сообщение могло быть удалено вручную или TG не позволяет
        # (старше 48ч в группе). Не критично — обнулим колонку.
        logger.debug(
            "purge_overdue_card: task #{} delete failed (msg={}): {}",
            task_id,
            msg_id,
            exc,
        )

    async with async_session_factory() as session:
        async with session.begin():
            await session.execute(
                update(Task).where(Task.id == task_id).values(overdue_message_id=None)
            )
