"""
OutboxService — публикация карточек задачи в Telegram с гарантированным
retry. Никаких «зависших в БД, но не запостившихся» задач: запись в outbox
ставится в той же транзакции, что и INSERT tasks, после чего либо
синхронный `process_now`, либо фоновый `process_pending` доводит дело
до конца.

Retry-лестница: 5с → 30с → 2м → 5м → 15м → 30м → failed. Первая
попытка — синхронно в `process_now` сразу после создания задачи.
Пермаментные ошибки (BadRequest, Forbidden) — failed сразу, без retry.

`failed` → DM-алерт владельцу (Михаилу) через `send_alert`. Юзер
никаких деталей не видит — у него в UI только «Задача создана» или
нейтральная ошибка от хэндлера.
"""

from __future__ import annotations

import html
from datetime import timedelta
from typing import Sequence

from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.keyboards.task_card import build_task_card_kb
from app.bot.utils.render import render_task_card
from app.bot.utils.tg import send_file_with_retry, send_with_retry
from app.db.base import async_session_factory
from app.db.models import TaskOutbox, User
from app.db.repositories.app_settings import (
    KEY_LEADERSHIP_TOPIC_ID,
    KEY_TASK_CHAT_ID,
    AppSettingsRepository,
)
from app.db.repositories.broadcasts import BroadcastsRepository
from app.db.repositories.outbox import OutboxRepository
from app.db.repositories.tasks import TasksRepository
from app.services.alerts import dispatch_alert


def _esc(s: str | None) -> str:
    return html.escape(s or "")


# Лестница задержек ПОСЛЕ N-ой неуспешной попытки.
# attempts=1 (после 1-го промаха) → ждём RETRY_SCHEDULE[0] = 5с до следующей.
# attempts=MAX_ATTEMPTS — переход в `failed`.
RETRY_SCHEDULE: list[timedelta] = [
    timedelta(seconds=5),
    timedelta(seconds=30),
    timedelta(minutes=2),
    timedelta(minutes=5),
    timedelta(minutes=15),
    timedelta(minutes=30),
]
MAX_ATTEMPTS = len(RETRY_SCHEDULE) + 1  # = 7 (включая первую синхронную)

# Пермаментные ошибки — те, что не уйдут сами по себе. Сразу failed.
_PERMANENT_TG_EXCEPTIONS = (TelegramBadRequest, TelegramForbiddenError)


class OutboxService:
    @staticmethod
    async def process_now(bot: Bot, task_id: int) -> bool:
        """Синхронно попытаться опубликовать все pending-записи задачи.
        True — все доставлены, False — хотя бы одна осталась в pending/failed."""
        async with async_session_factory() as session:
            async with session.begin():
                rows = await OutboxRepository.lock_pending_for_task(session, task_id)
                if not rows:
                    return True
                results = []
                for row in rows:
                    ok = await _process_one(bot, session, row)
                    results.append(ok)
        return all(results)

    @staticmethod
    async def process_pending(bot: Bot, limit: int = 50) -> int:
        """Фоновая обработка всех due-pending записей. Возвращает число
        обработанных (не обязательно успешно — счётчик попыток)."""
        async with async_session_factory() as session:
            async with session.begin():
                rows = await OutboxRepository.lock_due_pending(session, limit=limit)
                if not rows:
                    return 0
                for row in rows:
                    await _process_one(bot, session, row)
        return len(rows)


async def _process_one(bot: Bot, session: AsyncSession, row: TaskOutbox) -> bool:
    """Одна попытка публикации. На входе — заблокированная FOR UPDATE
    запись внутри активной транзакции. Сама транзакция коммитится снаружи."""
    try:
        if row.action == "post_dept":
            ok = await _post_dept(bot, session, row.task_id)
        elif row.action == "post_lead":
            ok = await _post_lead(bot, session, row.task_id)
        elif row.action == "broadcast":
            ok = await _broadcast(bot, session, row.task_id)
        elif row.action == "update_lead":
            ok = await _update_lead(bot, session, row.task_id)
        else:
            await OutboxRepository.mark_failed(
                session,
                row.id,
                error=f"unknown action: {row.action}",
                attempts=row.attempts + 1,
            )
            return False

        if not ok:
            # Конфиг отсутствует (нет TASK_CHAT_ID или topic_id) — действие
            # бессмысленно, помечаем failed и шлём алерт.
            await OutboxRepository.mark_failed(
                session,
                row.id,
                error="конфиг отсутствует (TASK_CHAT_ID или topic_id не задан)",
                attempts=row.attempts + 1,
            )
            dispatch_alert(
                bot,
                f"⚠️ task-bot: outbox#{row.id} (task_id={row.task_id}, "
                f"action={row.action}) — отказано из-за конфига. "
                "Проверь TASK_CHAT_ID/topic_id в app_settings.",
            )
            return False

        await OutboxRepository.mark_done(session, row.id)
        logger.info(
            "outbox: task_id={} action={} delivered (after {} attempts)",
            row.task_id,
            row.action,
            row.attempts + 1,
        )
        return True

    except _PERMANENT_TG_EXCEPTIONS as exc:
        attempts = row.attempts + 1
        msg = f"permanent TG error: {exc}"
        await OutboxRepository.mark_failed(session, row.id, error=msg, attempts=attempts)
        dispatch_alert(
            bot,
            f"⚠️ task-bot: outbox#{row.id} (task_id={row.task_id}, "
            f"action={row.action}) — пермаментная ошибка Telegram. "
            f"{exc}\n\nЗадача создана в БД, но в чат не опубликована. "
            "Проверь права бота в нужном топике или TASK_CHAT_ID.",
        )
        logger.error(
            "outbox: task_id={} action={} PERMANENT FAIL: {}",
            row.task_id,
            row.action,
            exc,
        )
        return False

    except TelegramRetryAfter as exc:
        # Telegram явно сказал когда повторить — уважаем.
        attempts = row.attempts + 1
        delay = timedelta(seconds=exc.retry_after + 1)
        if attempts >= MAX_ATTEMPTS:
            await OutboxRepository.mark_failed(
                session,
                row.id,
                error=f"max attempts reached after flood: {exc}",
                attempts=attempts,
            )
            dispatch_alert(
                bot,
                f"⚠️ task-bot: outbox#{row.id} (task_id={row.task_id}, "
                f"action={row.action}) — исчерпаны попытки на flood-control. "
                "Не публикуется уже больше часа.",
            )
            return False
        await OutboxRepository.reschedule(
            session,
            row.id,
            delay=delay,
            error=f"flood: retry_after={exc.retry_after}s",
            attempts=attempts,
        )
        return False

    except Exception as exc:  # noqa: BLE001
        attempts = row.attempts + 1
        if attempts >= MAX_ATTEMPTS:
            await OutboxRepository.mark_failed(
                session,
                row.id,
                error=f"max attempts: {exc}",
                attempts=attempts,
            )
            dispatch_alert(
                bot,
                f"⚠️ task-bot: outbox#{row.id} (task_id={row.task_id}, "
                f"action={row.action}) — исчерпаны {MAX_ATTEMPTS} попыток. "
                f"Последняя ошибка: {exc}",
            )
            return False
        delay = RETRY_SCHEDULE[attempts - 1]
        await OutboxRepository.reschedule(
            session, row.id, delay=delay, error=str(exc), attempts=attempts
        )
        logger.warning(
            "outbox: task_id={} action={} retry #{} in {}s ({})",
            row.task_id,
            row.action,
            attempts,
            int(delay.total_seconds()),
            exc,
        )
        return False


async def _post_dept(bot: Bot, session: AsyncSession, task_id: int) -> bool:
    """Постинг карточки задачи в топик отдела. Возвращает False, если
    конфиг не позволяет публиковать (нет TASK_CHAT_ID/topic_id)."""
    task = await TasksRepository.get_full(session, task_id)
    if task is None:
        raise RuntimeError(f"task {task_id} not found while posting")

    # Идемпотентность: если карточка уже опубликована (предыдущая попытка
    # успела до коммита БД-сессии или сюда зашёл повторный worker по гонке)
    # — не плодим дубль в чате, помечаем outbox-запись done.
    if task.dept_message_id is not None:
        logger.info(
            "outbox _post_dept: task #{} уже опубликована (msg_id={}), skip",
            task.id,
            task.dept_message_id,
        )
        return True

    task_chat_id = (await AppSettingsRepository.get_int(session, KEY_TASK_CHAT_ID)) or 0
    if task_chat_id == 0:
        return False
    if not task.department or not task.department.topic_id:
        return False
    topic_id = task.department.topic_id

    files = _orm_files_to_dicts(task.files)
    card_text = render_task_card(
        task,
        creator=task.creator,
        department=task.department,
        files_count=len(files),
    )
    kb = build_task_card_kb(task.status, task.id)

    msg = await send_with_retry(
        bot,
        chat_id=task_chat_id,
        text=card_text,
        reply_markup=kb,
        message_thread_id=topic_id,
    )
    await TasksRepository.set_message_ids(
        session,
        task.id,
        dept_chat_id=task_chat_id,
        dept_message_id=msg.message_id,
    )
    for f in files:
        try:
            await send_file_with_retry(
                bot,
                chat_id=task_chat_id,
                file_id=f["file_id"],
                kind=f.get("kind", "document"),
                file_name=f.get("file_name"),
                message_thread_id=topic_id,
            )
        except Exception as exc:  # noqa: BLE001
            # Файл — best-effort. Карточка опубликована, это главное.
            logger.warning("outbox: task#{} dept-file post failed: {}", task.id, exc)
    return True


async def _post_lead(bot: Bot, session: AsyncSession, task_id: int) -> bool:
    """Зеркало в топик «Руководство». False — если LEADERSHIP_TOPIC_ID
    не задан (это норма, не ошибка)."""
    task = await TasksRepository.get_full(session, task_id)
    if task is None:
        raise RuntimeError(f"task {task_id} not found while posting")

    # Идемпотентность — как в _post_dept.
    if task.arch_message_id is not None:
        logger.info(
            "outbox _post_lead: task #{} уже зеркалирована (msg_id={}), skip",
            task.id,
            task.arch_message_id,
        )
        return True

    task_chat_id = (await AppSettingsRepository.get_int(session, KEY_TASK_CHAT_ID)) or 0
    lead_topic_id = (await AppSettingsRepository.get_int(session, KEY_LEADERSHIP_TOPIC_ID)) or 0
    if task_chat_id == 0 or lead_topic_id == 0:
        return False

    files = _orm_files_to_dicts(task.files)
    card_text = render_task_card(
        task,
        creator=task.creator,
        department=task.department,
        files_count=len(files),
    )

    msg = await send_with_retry(
        bot,
        chat_id=task_chat_id,
        text=card_text,
        reply_markup=None,
        message_thread_id=lead_topic_id,
    )
    await TasksRepository.set_message_ids(session, task.id, arch_message_id=msg.message_id)
    for f in files:
        try:
            await send_file_with_retry(
                bot,
                chat_id=task_chat_id,
                file_id=f["file_id"],
                kind=f.get("kind", "document"),
                file_name=f.get("file_name"),
                message_thread_id=lead_topic_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("outbox: task#{} lead-file post failed: {}", task.id, exc)
    return True


def _orm_files_to_dicts(files: Sequence, *, purpose: str | None = None) -> list[dict]:
    """ORM-объекты TaskFile → dict для send_file_with_retry.

    purpose=None — все файлы; purpose='creation'|'completion' — фильтр.
    """
    return [
        {
            "file_id": f.tg_file_id,
            "file_name": f.file_name,
            "kind": f.kind,
            "mime_type": f.mime_type,
        }
        for f in files
        if purpose is None or getattr(f, "purpose", "creation") == purpose
    ]


async def _update_lead(bot: Bot, session: AsyncSession, task_id: int) -> bool:
    """Обновление зеркала задачи в топике «Руководство» при смене статуса.

    Шаги:
      1. Если у задачи ещё нет arch_message_id (зеркало не публиковалось,
         например пост_lead не успел или leadership_topic_id не настроен) —
         возвращаем True (нечего обновлять, не блокируем жизненный цикл).
      2. Иначе edit_message_text карточки.
      3. Если status='done' и completion-файлы ещё не пересланы в лид —
         досылаем их reply'ом на зеркало и ставим lead_completion_posted=True.

    «message is not modified» от Telegram — нормальный исход, считаем done.
    """
    task = await TasksRepository.get_full(session, task_id)
    if task is None:
        raise RuntimeError(f"task {task_id} not found while updating lead")

    if task.arch_message_id is None:
        logger.info(
            "outbox _update_lead: task #{} без arch_message_id — нечего обновлять",
            task.id,
        )
        return True

    task_chat_id = (await AppSettingsRepository.get_int(session, KEY_TASK_CHAT_ID)) or 0
    lead_topic_id = (await AppSettingsRepository.get_int(session, KEY_LEADERSHIP_TOPIC_ID)) or 0
    if task_chat_id == 0 or lead_topic_id == 0:
        return False

    all_files = _orm_files_to_dicts(task.files)
    card_text = render_task_card(
        task,
        creator=task.creator,
        assignee=task.assignee,
        department=task.department,
        files_count=len(all_files),
    )
    try:
        await bot.edit_message_text(
            chat_id=task_chat_id,
            message_id=task.arch_message_id,
            text=card_text,
        )
    except TelegramBadRequest as exc:
        # «message is not modified» — пишет когда текст идентичен. Это норма
        # при повторных update_lead (например, accept + accept-side-effect).
        if "not modified" in str(exc).lower():
            logger.debug("outbox _update_lead: task #{} текст не изменился, skip", task.id)
        else:
            raise

    if task.status == "done" and not task.lead_completion_posted:
        completion_files = _orm_files_to_dicts(task.files, purpose="completion")
        for f in completion_files:
            try:
                await send_file_with_retry(
                    bot,
                    chat_id=task_chat_id,
                    file_id=f["file_id"],
                    kind=f.get("kind", "document"),
                    file_name=f.get("file_name"),
                    message_thread_id=lead_topic_id,
                    reply_to_message_id=task.arch_message_id,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "outbox _update_lead: task #{} completion-file post failed: {}",
                    task.id,
                    exc,
                )
        task.lead_completion_posted = True
        await session.flush()

    return True


async def post_event_to_lead(
    bot: Bot,
    task_id: int,
    *,
    kind: str,
    by_user_name: str,
    comment: str | None,
    files: list[dict] | None = None,
) -> None:
    """Досылает в зеркало «Руководство» событие задачи как reply на её карточку.

    kind: 'submission' (исполнитель отправил отчёт), 'rework' (постановщик
    вернул на доработку), 'question' (исполнитель задал вопрос), 'answer'
    (постановщик ответил на вопрос). При отсутствии arch_message_id или
    нерасставленных KEY_TASK_CHAT_ID/KEY_LEADERSHIP_TOPIC_ID — тихо выходит
    (это не ошибка, просто зеркала нет). Best-effort, не блокирует основной
    flow задачи.
    """
    label = {
        "submission": "📨 Ответ исполнителя",
        "rework": "✏️ Возвращено на доработку",
        "question": "❓ Вопрос от исполнителя",
        "answer": "💬 Ответ постановщика",
        "reassign": "🔄 Передана другому исполнителю",
    }.get(kind, f"• {kind}")
    try:
        async with async_session_factory() as session:
            task_chat_id = (await AppSettingsRepository.get_int(session, KEY_TASK_CHAT_ID)) or 0
            lead_topic_id = (
                await AppSettingsRepository.get_int(session, KEY_LEADERSHIP_TOPIC_ID)
            ) or 0
            if task_chat_id == 0 or lead_topic_id == 0:
                return
            task = await TasksRepository.get_full(session, task_id)
            if task is None or task.arch_message_id is None:
                return
            reply_to = task.arch_message_id

        header_lines = [
            f"{label} · #{task.display_number} · {_esc(by_user_name)}",
        ]
        if comment:
            header_lines.append("")
            header_lines.append(_esc(comment[:1500]))
        text = "\n".join(header_lines)
        await send_with_retry(
            bot,
            chat_id=task_chat_id,
            text=text,
            message_thread_id=lead_topic_id,
            reply_to_message_id=reply_to,
        )
        for f in files or []:
            try:
                await send_file_with_retry(
                    bot,
                    chat_id=task_chat_id,
                    file_id=f["file_id"],
                    kind=f.get("kind", "document"),
                    file_name=f.get("file_name"),
                    message_thread_id=lead_topic_id,
                    reply_to_message_id=reply_to,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "post_event_to_lead: task #{} file post failed ({}): {}",
                    task_id,
                    kind,
                    exc,
                )
    except Exception as exc:  # noqa: BLE001
        logger.warning("post_event_to_lead failed (task #{}, kind={}): {}", task_id, kind, exc)


async def _broadcast(bot: Bot, session: AsyncSession, task_id: int) -> bool:
    """
    Рассылает карточку задачи каждому активному approved-участнику
    направления (department_id задачи) в личку и регистрирует доставку
    в task_broadcasts. Сам постановщик из рассылки исключается — он
    инициатор и так знает что поставил.

    Этап А «Бот без чата»: заменяет публикацию в топик отдела
    (_post_dept) и зеркалирование в «Руководство» (_post_lead).

    Идемпотентность:
      - UNIQUE (task_id, user_id) на task_broadcasts защищает от дублей
        при повторной попытке (retry или гонка двух worker'ов).
      - Если у получателя уже есть запись broadcast'а — повторно не шлём.
    Возвращает True всегда (если БД жива). Не доставленные DM (Forbidden:
    юзер не нажимал /start, заблокировал бота) попадают в логи и в DM-сводку
    постановщику, но broadcast как outbox-задача считается выполненным —
    повторять её бессмысленно, ситуация не «само починится».
    """
    from sqlalchemy import or_ as sa_or_, select as sa_select  # лок. чтобы не плодить алиасы

    task = await TasksRepository.get_full(session, task_id)
    if task is None:
        raise RuntimeError(f"task {task_id} not found while broadcasting")

    # Принимать задачу пока нельзя только если она в new/in_progress;
    # broadcast после accept бессмыслен (карточка уже на руках).
    if task.status not in ("new",):
        logger.info(
            "outbox _broadcast: task #{} в статусе {} — broadcast пропущен",
            task.id,
            task.status,
        )
        return True

    # Получатели рассылки:
    # 1) активные approved-сотрудники направления (классический поток),
    # 2) ВСЕ активные approved-админы — независимо от department_id.
    #    Админу нужно видеть каждую новую задачу в личке, чтобы держать
    #    общую картину работы (Алёна попросила 28.05.2026).
    # Из обоих списков исключаем самого creator'а — он сам поставил.
    recipients = (
        (
            await session.execute(
                sa_select(User)
                .where(
                    User.is_active.is_(True),
                    User.access_status == "approved",
                    User.id != task.creator_id,
                    sa_or_(
                        User.department_id == task.department_id,
                        User.role == "admin",
                    ),
                )
                .distinct()
            )
        )
        .scalars()
        .all()
    )

    if not recipients:
        logger.warning(
            "outbox _broadcast: task #{} в отделе department_id={} нет "
            "получателей кроме постановщика — карточка никому не уйдёт",
            task.id,
            task.department_id,
        )
        # Алерт постановщику, чтобы понимал почему задача висит без приёма.
        try:
            await send_with_retry(
                bot,
                chat_id=task.creator.tg_user_id,
                text=(
                    f"⚠️ Задача #{task.display_number} «{_esc(task.title)}» отправлена в "
                    f"направление «{_esc(task.department.name)}», но в нём пока нет "
                    "активных участников кроме тебя. Никто не получит карточку."
                ),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("broadcast: notify creator (no recipients) failed: {}", exc)
        return True

    files = _orm_files_to_dicts(task.files)
    card_text = render_task_card(
        task,
        creator=task.creator,
        department=task.department,
        files_count=len(files),
    )
    kb = build_task_card_kb(task.status, task.id)

    delivered: list[str] = []
    undelivered: list[str] = []

    for u in recipients:
        # Идемпотентность: если запись для (task_id, user_id) уже есть —
        # broadcast этому юзеру уже состоялся в предыдущем заходе, не дублим.
        existing = await BroadcastsRepository.get_for_user(session, task_id=task.id, user_id=u.id)
        if existing is not None:
            continue
        try:
            msg = await send_with_retry(bot, chat_id=u.tg_user_id, text=card_text, reply_markup=kb)
        except TelegramRetryAfter:
            # На flood — отдаём всю outbox-запись в retry, чтобы дожать
            # остальных получателей позже (а не доставлять «частично, но сейчас»).
            raise
        except (TelegramBadRequest, TelegramForbiddenError) as exc:
            # Юзер не нажимал /start, заблокировал бота, удалил аккаунт.
            # Повторять бесполезно — фиксируем в логи + в сводку.
            logger.warning(
                "broadcast: task#{} DM to user_id={} (tg={}) failed: {}",
                task.id,
                u.id,
                u.tg_user_id,
                exc,
            )
            label = (
                f"@{_esc(u.tg_username)}"
                if u.tg_username
                else (_esc(u.full_name) or f"id={u.tg_user_id}")
            )
            undelivered.append(label)
            continue
        await BroadcastsRepository.add(
            session,
            task_id=task.id,
            user_id=u.id,
            tg_user_id=u.tg_user_id,
            message_id=msg.message_id,
        )
        # Прикреплённые файлы шлём следом (best-effort, не блокируют delivery).
        for f in files:
            try:
                await send_file_with_retry(
                    bot,
                    chat_id=u.tg_user_id,
                    file_id=f["file_id"],
                    kind=f.get("kind", "document"),
                    file_name=f.get("file_name"),
                )
            except Exception as file_exc:  # noqa: BLE001
                logger.warning(
                    "broadcast: task#{} file to user_id={} failed: {}",
                    task.id,
                    u.id,
                    file_exc,
                )
        label = (
            f"@{_esc(u.tg_username)}"
            if u.tg_username
            else (_esc(u.full_name) or f"id={u.tg_user_id}")
        )
        delivered.append(label)

    # Сводка постановщику. Без неё он не понимает охват рассылки.
    summary_lines = [
        f"📤 Задача #{task.display_number} «{_esc(task.title)}» разослана в «{_esc(task.department.name)}»."
    ]
    if delivered:
        summary_lines.append(f"Получили ({len(delivered)}): " + ", ".join(delivered))
    if undelivered:
        summary_lines.append(
            f"Не доставлено ({len(undelivered)}): "
            + ", ".join(undelivered)
            + ".\nЭти пользователи ещё не открывали бота в личке или заблокировали его."
        )
    try:
        await send_with_retry(bot, chat_id=task.creator.tg_user_id, text="\n\n".join(summary_lines))
    except Exception as exc:  # noqa: BLE001
        logger.warning("broadcast: notify creator summary failed: {}", exc)

    return True
