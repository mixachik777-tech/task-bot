"""
Действия с задачей: Принять / Завершить / Отменить / Комментарий.

Каждое действие — одна транзакция БД:
  SELECT FOR UPDATE → проверка статуса → UPDATE → INSERT history → sync TG.

Защита от двойного клика — через TasksRepository.lock_for_update.
Переходы статусов — только из ALLOWED_STATUS_TRANSITIONS (enums.py).
На запретный переход поднимается InvalidStatusTransition.

Sync TG карточки в обоих топиках:
  - dept-сообщение: editMessageText с актуальной клавиатурой. Если editMessage
    падает с реальной BadRequest (не «not modified» / «message gone») — raise,
    транзакция откатывается, юзеру alert «попробуй позже».
  - arch-сообщение в «Руководстве»: editMessageText с reply_markup=None,
    best-effort. Ошибка → WARNING, не откатываем (зеркало некритично).

Уведомления постановщику/исполнителю — ПОСЛЕ commit, через send_dm_safe
(best-effort, не падают и не ретраются для упрощения).
"""

import asyncio
import html
from dataclasses import dataclass, field
from typing import Any

from aiogram import Bot
from loguru import logger

from app.bot.keyboards.task_card import (
    build_approval_kb,
    build_complete_action_kb,
    build_task_card_kb,
)
from app.bot.utils.render import render_task_card
from app.bot.utils.tg import edit_text_safe, send_dm_safe, send_file_with_retry
from app.bot.utils.time import now_utc
from app.db.base import async_session_factory
from app.db.enums import (
    ALLOWED_STATUS_TRANSITIONS,
    HistoryEventType,
    TaskStatus,
    UserRole,
)
from app.db.exceptions import InvalidStatusTransition
from app.db.models import Department, Task, TaskBroadcast, User
from app.db.repositories.broadcasts import BroadcastsRepository
from app.db.repositories.departments import DepartmentsRepository
from app.db.repositories.history import HistoryRepository
from app.db.repositories.outbox import OutboxRepository
from app.db.repositories.tasks import TasksRepository
from app.db.repositories.users import UsersRepository
from app.scheduler.bootstrap import (
    remove_reminders_for_task,
    schedule_reminders_for_task,
)
from app.scheduler.runtime import get_scheduler
from app.services.outbox_service import OutboxService


def _esc(s: str | None) -> str:
    """HTML-escape для пользовательского текста. Бот стартует с parse_mode=HTML,
    поэтому любое поле от юзера (title, comment, full_name, tg_username) перед
    подстановкой в f-строку обязано пройти через это. Иначе сломанный тег
    (<b без закрытия) обрушит отправку уведомления, а валидный <a href=...>
    превращается в фишинговую ссылку."""
    return html.escape(s or "")


# Если зеркало в Руководстве не успело обновиться синхронно после
# смены статуса (например, Telegram тормозит) — фоновый outbox-воркер
# дожмёт. Юзера тут не блокируем.
LEAD_UPDATE_SYNC_TIMEOUT_S = 5


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────


@dataclass
class ActionResult:
    """Результат любого действия с задачей.

    user_message — то, что показать инициатору в alert/answer.
    notifications — список dict'ов вида:
      {"kind": "text", "chat_id": int, "text": str}
      {"kind": "document"|"photo"|"video"|"animation",
       "chat_id": int, "file_id": str, "file_name": str | None,
       "caption": str | None}
    Шлются после commit, по одному, best-effort (не падают наружу).
    """

    success: bool
    user_message: str
    task: Task | None = None
    notifications: list[dict[str, Any]] = field(default_factory=list)


def _user_handle(user: User | None) -> str:
    """Возвращает уже-экранированный handle для подстановки в HTML-сообщение."""
    if user is None:
        return "—"
    if user.tg_username:
        return f"@{_esc(user.tg_username)}"
    return _esc(user.full_name) or f"id={user.tg_user_id}"


async def _update_lead_in_background(bot: Bot, task_id: int) -> None:
    """Фоновая корутина: enqueue update_lead + попытка sync-push.

    Вызывается через `asyncio.create_task` из `_try_update_lead_mirror`, чтобы
    callback пользователю не висел, пока редактируется зеркало в Руководстве
    (TG-вызов edit_message_text + потенциально send file могут занимать
    секунды и упирали callback в Telegram-таймаут «нет ответа»).
    """
    try:
        async with async_session_factory() as session:
            async with session.begin():
                await OutboxRepository.enqueue_idempotent(session, task_id, "update_lead")
    except Exception as exc:  # noqa: BLE001
        # Реальная DB-ошибка (коннект потерян и т.п.). Логируем на WARNING.
        # Не уходим: основная транзакция action'а уже атомарно поставила
        # update_lead в outbox (см. enqueue_idempotent в accept/approve/
        # request_rework/cancel/complete/reassign/comment), фоновый воркер
        # подхватит. ON CONFLICT гарантирует, что повтор тут — no-op.
        logger.warning("update_lead fire-and-forget enqueue failed for task #{}: {}", task_id, exc)

    try:
        await asyncio.wait_for(
            OutboxService.process_now(bot, task_id),
            timeout=LEAD_UPDATE_SYNC_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        logger.debug(
            "update_lead sync push timed out for task #{} — background worker takes over",
            task_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("update_lead sync push failed for task #{}: {}", task_id, exc)


def _try_update_lead_mirror(bot: Bot, task_id: int) -> None:
    """Запустить обновление зеркала в Руководстве в фоне (fire-and-forget).

    Не блокирует callback хендлера: пользователь моментально получает ответ
    на «Готово, завершить» / «Согласовано» / «На доработку», а зеркало
    обновляется параллельно. Если фон. таска упадёт — outbox-воркер
    (каждую минуту) дожмёт.
    """
    asyncio.create_task(_update_lead_in_background(bot, task_id))


def _try_purge_overdue(bot: Bot, task_id: int) -> None:
    """Fire-and-forget — убрать карточку из топика «Просроченные».

    Вызывается при complete / approve / cancel. Если задача никогда не
    была в топике (overdue_message_id NULL) — функция тихо ничего не
    делает.
    """
    from app.services.overdue_topic import purge_overdue_card

    asyncio.create_task(purge_overdue_card(bot, task_id))


def _try_post_event_to_lead(
    bot: Bot,
    task_id: int,
    *,
    kind: str,
    by_user_name: str,
    comment: str | None,
    files: list[dict[str, Any]] | None = None,
) -> None:
    """Fire-and-forget — досылка события (ответ/правки/вопрос/ответ) в зеркало.

    Используется чтобы в топике «Руководство» сразу была видна переписка
    по задаче, а не только финальный approve. При отсутствии настроек
    зеркала (KEY_LEADERSHIP_TOPIC_ID не задан) — тихо игнорирует.
    """
    from app.services.outbox_service import post_event_to_lead

    asyncio.create_task(
        post_event_to_lead(
            bot,
            task_id,
            kind=kind,
            by_user_name=by_user_name,
            comment=comment,
            files=files,
        )
    )


def _assert_transition(from_status: str, to_status: TaskStatus) -> None:
    try:
        from_enum = TaskStatus(from_status)
    except ValueError as exc:
        raise InvalidStatusTransition(from_status, to_status.value) from exc
    if (from_enum, to_status) not in ALLOWED_STATUS_TRANSITIONS:
        raise InvalidStatusTransition(from_status, to_status.value)


@dataclass
class CardSyncSnap:
    """Снимок параметров для синхронизации TG-карточки задачи.

    Собирается ВНУТРИ session.begin() (требует ORM-атрибуты), применяется
    через _apply_card_snapshot ПОСЛЕ commit'a — чтобы edit_message_text в
    Telegram не выполнялся раньше, чем БД-транзакция закоммитится.
    Inverted order приводит к рассинхрону: если транзакция упадёт (любая
    БД-ошибка) — TG уже отредактирован, а БД откатилась, kb-кнопки в чате
    остаются от несостоявшегося статуса.
    """

    chat_id: int
    dept_message_id: int | None
    arch_message_id: int | None
    text: str
    kb: Any  # InlineKeyboardMarkup | None — Any чтобы не тянуть aiogram-тип сюда


def _build_card_snapshot(
    task: Task,
    *,
    creator: User,
    assignee: User | None,
    department: Department | None,
    files_count: int,
) -> CardSyncSnap | None:
    """Собрать снимок данных для TG-карточки ВНУТРИ транзакции.

    Возвращает None если задача в БД-only режиме (нет dept_chat_id) —
    синхронизировать нечего.
    """
    if not task.dept_chat_id:
        return None
    text = render_task_card(
        task,
        creator=creator,
        assignee=assignee,
        department=department,
        files_count=files_count,
    )
    kb = build_task_card_kb(task.status, task.id)
    return CardSyncSnap(
        chat_id=task.dept_chat_id,
        dept_message_id=task.dept_message_id,
        arch_message_id=task.arch_message_id,
        text=text,
        kb=kb,
    )


async def _apply_card_snapshot(bot: Bot, snap: CardSyncSnap | None, task_id: int) -> None:
    """Применить снимок TG-карточки ПОСЛЕ commit'a.

    Best-effort: TG-ошибки логируются, но НЕ пробрасываются — БД уже
    committed, откатывать нельзя, иначе вернёмся к старому рассинхрону.
    """
    if snap is None:
        return
    if snap.dept_message_id:
        try:
            await edit_text_safe(
                bot,
                chat_id=snap.chat_id,
                message_id=snap.dept_message_id,
                text=snap.text,
                reply_markup=snap.kb,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — после commit нельзя падать
            logger.warning("after-commit live card sync failed for task #{}: {}", task_id, exc)
    if snap.arch_message_id:
        try:
            await edit_text_safe(
                bot,
                chat_id=snap.chat_id,
                message_id=snap.arch_message_id,
                text=snap.text,
                reply_markup=None,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("after-commit arch mirror sync failed for task #{}: {}", task_id, exc)


# ──────────────────────────────────────────────────────────────────────
# Service
# ──────────────────────────────────────────────────────────────────────


class TaskActionsService:
    @staticmethod
    async def accept(*, bot: Bot, task_id: int, user: User) -> ActionResult:
        """
        Берёт задачу в работу. Двойной клик защищён SELECT FOR UPDATE:
        второй клик увидит status='in_progress' и получит отказ.
        """
        result = ActionResult(success=False, user_message="")
        broadcast_cleanup: list[TaskBroadcast] = []
        card_snap: CardSyncSnap | None = None
        async with async_session_factory() as session:
            async with session.begin():
                task = await TasksRepository.lock_for_update(session, task_id)
                if task is None:
                    return ActionResult(False, "Задача не найдена.")

                if task.status == TaskStatus.IN_PROGRESS.value:
                    cur_assignee = (
                        await UsersRepository.get_by_id(session, task.assignee_id)
                        if task.assignee_id
                        else None
                    )
                    return ActionResult(
                        False,
                        f"Задача уже в работе у {_user_handle(cur_assignee)}.",
                    )
                if task.status != TaskStatus.NEW.value:
                    return ActionResult(
                        False,
                        f"Задача в статусе «{task.status}» — взять нельзя.",
                    )

                _assert_transition(task.status, TaskStatus.IN_PROGRESS)
                task.status = TaskStatus.IN_PROGRESS.value
                task.assignee_id = user.id
                task.accepted_at = now_utc()

                # Этап А. Если задача жила в DM-режиме (broadcast в личку,
                # а не пост в топик) — у нас в task_broadcasts по строке
                # на каждого получателя. Карточку принявшего переводим в
                # «активную» (записываем её chat/message_id в dept_chat_id/
                # dept_message_id — карточка-снимок работает с этими полями),
                # карточки остальных удаляем из их личек (после commit'a).
                if not task.dept_message_id:
                    own_bcast = await BroadcastsRepository.get_for_user(
                        session, task_id=task.id, user_id=user.id
                    )
                    if own_bcast is not None:
                        task.dept_chat_id = own_bcast.tg_user_id
                        task.dept_message_id = own_bcast.message_id
                    broadcast_cleanup = await BroadcastsRepository.delete_others(
                        session, task_id=task.id, keep_user_id=user.id
                    )
                await session.flush()

                await HistoryRepository.log(
                    session,
                    task_id=task.id,
                    user_id=user.id,
                    event_type=HistoryEventType.ACCEPTED,
                )

                creator = await UsersRepository.get_by_id(session, task.creator_id)
                department = await DepartmentsRepository.get_by_id(session, task.department_id)
                files_count = await TasksRepository.count_files(session, task.id)
                # Снимок параметров TG-карточки — применим ПОСЛЕ commit'a.
                # Раньше здесь стоял await _sync_card(...), который слал
                # edit_message_text прямо в TG ВНУТРИ транзакции. При
                # любом падении транзакции (например, outbox enqueue
                # сломался в 2026-06-02 из-за рассинхрона партиального
                # индекса) TG-сообщение уже было отредактировано, а БД
                # откатывалась — kb в чате юзера показывал «in_progress»
                # на задаче со статусом «new» в БД. См. feedback_*.
                card_snap = _build_card_snapshot(
                    task,
                    creator=creator,
                    assignee=user,
                    department=department,
                    files_count=files_count,
                )

                # Атомарно ставим update_lead в outbox в той же транзакции,
                # что и смена статуса. Если процесс упадёт после commit'а
                # main txn, но до fire-and-forget create_task — периодический
                # outbox-воркер всё равно подхватит. ON CONFLICT гарантирует
                # no-op при повторной постановке.
                await OutboxRepository.enqueue_idempotent(session, task.id, "update_lead")

                result.success = True
                result.user_message = "Вы взяли задачу в работу."
                result.task = task
                if creator and creator.id != user.id:
                    result.notifications.append(
                        {
                            "kind": "text",
                            "chat_id": creator.tg_user_id,
                            "text": f"🟡 {_user_handle(user)} взял(а) задачу #{task.display_number} в работу.",
                        }
                    )
                # Уведомляем остальных получателей коротким текстом, чтобы
                # они понимали почему карточка пропала из лички.
                for bcast in broadcast_cleanup:
                    result.notifications.append(
                        {
                            "kind": "text",
                            "chat_id": bcast.tg_user_id,
                            "text": (
                                f"ℹ️ Задачу #{task.display_number} «{_esc(task.title)}» уже "
                                f"взял в работу {_user_handle(user)}. "
                                "Карточка убрана из чата."
                            ),
                        }
                    )

        # ── ВНЕ session.begin(): commit прошёл, можно трогать TG ──
        # Sync «живой» карточки → новый kb (in_progress).
        if result.success:
            await _apply_card_snapshot(bot, card_snap, task_id)

        # Удаляем карточки в личках бывших получателей. Best-effort: если
        # юзер уже удалил сообщение или заблокировал бота — пропускаем.
        for bcast in broadcast_cleanup:
            try:
                await bot.delete_message(chat_id=bcast.tg_user_id, message_id=bcast.message_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "accept-cleanup: delete_message chat={} msg={} failed: {}",
                    bcast.tg_user_id,
                    bcast.message_id,
                    exc,
                )

        await _send_notifications(bot, result.notifications)
        if result.success:
            _try_update_lead_mirror(bot, task_id)
        return result

    @staticmethod
    async def complete(
        *,
        bot: Bot,
        task_id: int,
        user: User,
        result_comment: str | None,
        result_files: list[dict[str, Any]] | None = None,
    ) -> ActionResult:
        """
        Этап Б «Согласование». «✅ Завершить» исполнителя больше не закрывает
        задачу, а отправляет её на согласование постановщику: статус →
        awaiting_approval. Закрытие происходит после клика «✅ Согласовано»
        в подтверждающем сообщении у постановщика (см. `approve`).

        Право: только assignee или admin.
        """
        result_files = result_files or []
        result = ActionResult(success=False, user_message="")
        card_snap: CardSyncSnap | None = None
        async with async_session_factory() as session:
            async with session.begin():
                task = await TasksRepository.lock_for_update(session, task_id)
                if task is None:
                    return ActionResult(False, "Задача не найдена.")

                if not _can_complete(task, user):
                    return ActionResult(
                        False, "Отправлять отчёт может только исполнитель или админ."
                    )

                if task.status == TaskStatus.DONE.value:
                    return ActionResult(False, "Задача уже закрыта.")
                if task.status == TaskStatus.AWAITING_APPROVAL.value:
                    return ActionResult(False, "Задача уже на согласовании у постановщика.")
                if task.status != TaskStatus.IN_PROGRESS.value:
                    return ActionResult(
                        False,
                        f"Задача в статусе «{task.status}» — отчёт отправить нельзя.",
                    )

                _assert_transition(task.status, TaskStatus.AWAITING_APPROVAL)
                task.status = TaskStatus.AWAITING_APPROVAL.value
                await session.flush()

                # Сохраняем итоговые файлы исполнителя в task_files с
                # purpose='completion'. Это даёт зеркалу в Руководстве и
                # админскому «Все задачи» возможность отличить «итог» от
                # «материалов постановщика», а также гарантирует доставку
                # итога в Руководство после approve (см. _update_lead).
                if result_files:
                    await TasksRepository.add_files(
                        session, task.id, result_files, purpose="completion"
                    )

                payload: dict[str, Any] = {}
                if result_comment:
                    payload["result_comment"] = result_comment
                if result_files:
                    payload["result_files"] = result_files
                await HistoryRepository.log(
                    session,
                    task_id=task.id,
                    user_id=user.id,
                    event_type=HistoryEventType.SUBMITTED_FOR_APPROVAL,
                    payload=payload or None,
                )

                creator = await UsersRepository.get_by_id(session, task.creator_id)
                assignee = (
                    await UsersRepository.get_by_id(session, task.assignee_id)
                    if task.assignee_id
                    else None
                )
                department = await DepartmentsRepository.get_by_id(session, task.department_id)
                files_count = await TasksRepository.count_files(session, task.id)
                card_snap = _build_card_snapshot(
                    task,
                    creator=creator,
                    assignee=assignee,
                    department=department,
                    files_count=files_count,
                )

                # Атомарно ставим update_lead в outbox в той же транзакции,
                # что и смена статуса. Если процесс упадёт после commit'а
                # main txn, но до fire-and-forget create_task — периодический
                # outbox-воркер всё равно подхватит. ON CONFLICT гарантирует
                # no-op при повторной постановке.
                await OutboxRepository.enqueue_idempotent(session, task.id, "update_lead")

                result.success = True
                result.user_message = "📨 Отчёт отправлен постановщику на согласование."
                result.task = task

                if creator and creator.id != user.id:
                    dept_name: str | None = None
                    if task.department_id:
                        dept = await DepartmentsRepository.get_by_id(session, task.department_id)
                        dept_name = dept.name if dept else None
                    _build_completion_notifications(
                        notifications=result.notifications,
                        creator_tg_id=creator.tg_user_id,
                        executor=user,
                        task_id=task.id,
                        display_number=task.display_number,
                        task_title=task.title or "",
                        dept_name=dept_name,
                        comment=result_comment,
                        files=result_files,
                        attach_approval_kb=True,
                    )

        # ── ВНЕ session.begin(): commit прошёл, TG-side-effects ниже ──
        if result.success:
            await _apply_card_snapshot(bot, card_snap, task_id)
            _drop_reminders(task_id)
        await _send_notifications(bot, result.notifications)
        if result.success:
            _try_update_lead_mirror(bot, task_id)
            _try_purge_overdue(bot, task_id)
            _try_post_event_to_lead(
                bot,
                task_id,
                kind="submission",
                by_user_name=(user.full_name or _user_handle(user)),
                comment=result_comment,
                files=result_files or [],
            )
        return result

    @staticmethod
    async def approve(*, bot: Bot, task_id: int, user: User) -> ActionResult:
        """
        Этап Б. «✅ Согласовано» постановщика → задача закрывается.
        Право: creator или admin.
        """
        result = ActionResult(success=False, user_message="")
        card_snap: CardSyncSnap | None = None
        async with async_session_factory() as session:
            async with session.begin():
                task = await TasksRepository.lock_for_update(session, task_id)
                if task is None:
                    return ActionResult(False, "Задача не найдена.")

                if not _can_approve(task, user):
                    return ActionResult(
                        False, "Согласовать задачу может только постановщик или админ."
                    )

                if task.status == TaskStatus.DONE.value:
                    return ActionResult(False, "Задача уже закрыта.")
                if task.status != TaskStatus.AWAITING_APPROVAL.value:
                    return ActionResult(
                        False,
                        f"Задача в статусе «{task.status}» — согласовать нельзя.",
                    )

                _assert_transition(task.status, TaskStatus.DONE)
                task.status = TaskStatus.DONE.value
                task.completed_at = now_utc()
                await session.flush()

                await HistoryRepository.log(
                    session,
                    task_id=task.id,
                    user_id=user.id,
                    event_type=HistoryEventType.APPROVED,
                )

                creator = await UsersRepository.get_by_id(session, task.creator_id)
                assignee = (
                    await UsersRepository.get_by_id(session, task.assignee_id)
                    if task.assignee_id
                    else None
                )
                department = await DepartmentsRepository.get_by_id(session, task.department_id)
                files_count = await TasksRepository.count_files(session, task.id)
                card_snap = _build_card_snapshot(
                    task,
                    creator=creator,
                    assignee=assignee,
                    department=department,
                    files_count=files_count,
                )

                # Атомарно ставим update_lead в outbox в той же транзакции,
                # что и смена статуса. Если процесс упадёт после commit'а
                # main txn, но до fire-and-forget create_task — периодический
                # outbox-воркер всё равно подхватит. ON CONFLICT гарантирует
                # no-op при повторной постановке.
                await OutboxRepository.enqueue_idempotent(session, task.id, "update_lead")

                result.success = True
                result.user_message = "✅ Задача согласована и закрыта."
                result.task = task

                if assignee and assignee.id != user.id:
                    result.notifications.append(
                        {
                            "kind": "text",
                            "chat_id": assignee.tg_user_id,
                            "text": (
                                f"✅ Постановщик согласовал твою работу по задаче "
                                f"#{task.display_number} «{_esc(task.title)}». Спасибо!"
                            ),
                        }
                    )

        # ── ВНЕ session.begin(): commit прошёл, TG-side-effects ниже ──
        if result.success:
            await _apply_card_snapshot(bot, card_snap, task_id)
            _drop_reminders(task_id)
        await _send_notifications(bot, result.notifications)
        if result.success:
            _try_update_lead_mirror(bot, task_id)
            _try_purge_overdue(bot, task_id)
        return result

    @staticmethod
    async def request_rework(
        *,
        bot: Bot,
        task_id: int,
        user: User,
        comment: str | None = None,
        rework_files: list[dict[str, Any]] | None = None,
    ) -> ActionResult:
        """
        Этап Б. «✏️ На доработку» постановщика → задача снова in_progress
        у того же исполнителя. Этап В добавит обязательный comment.

        Право: creator или admin.
        """
        rework_files = rework_files or []
        result = ActionResult(success=False, user_message="")
        card_snap: CardSyncSnap | None = None
        async with async_session_factory() as session:
            async with session.begin():
                task = await TasksRepository.lock_for_update(session, task_id)
                if task is None:
                    return ActionResult(False, "Задача не найдена.")

                if not _can_approve(task, user):
                    return ActionResult(
                        False,
                        "Вернуть задачу на доработку может только постановщик или админ.",
                    )

                if task.status != TaskStatus.AWAITING_APPROVAL.value:
                    return ActionResult(
                        False,
                        f"Задача в статусе «{task.status}» — отправить на доработку нельзя.",
                    )

                _assert_transition(task.status, TaskStatus.IN_PROGRESS)
                task.status = TaskStatus.IN_PROGRESS.value
                # completed_at не выставляем — это IN_PROGRESS, не финал.
                # Сбрасываем reminder-флаги: цикл «complete → rework» начинает
                # новый раунд работы, прошлые напоминания «d2/d1/h2 отправлены»
                # должны посчитаться заново. Без сброса send_reminder
                # SKIP'нет всё из-за flag_already_set=True (см. jobs.py:60).
                task.reminder_2d_sent = False
                task.reminder_1d_sent = False
                task.reminder_2h_sent = False
                await session.flush()

                payload: dict[str, Any] = {}
                if comment:
                    payload["comment"] = comment
                if rework_files:
                    payload["rework_files"] = rework_files
                await HistoryRepository.log(
                    session,
                    task_id=task.id,
                    user_id=user.id,
                    event_type=HistoryEventType.REWORK_REQUESTED,
                    payload=payload or None,
                )

                creator = await UsersRepository.get_by_id(session, task.creator_id)
                assignee = (
                    await UsersRepository.get_by_id(session, task.assignee_id)
                    if task.assignee_id
                    else None
                )
                department = await DepartmentsRepository.get_by_id(session, task.department_id)
                files_count = await TasksRepository.count_files(session, task.id)
                card_snap = _build_card_snapshot(
                    task,
                    creator=creator,
                    assignee=assignee,
                    department=department,
                    files_count=files_count,
                )

                # Атомарно ставим update_lead в outbox в той же транзакции,
                # что и смена статуса. Если процесс упадёт после commit'а
                # main txn, но до fire-and-forget create_task — периодический
                # outbox-воркер всё равно подхватит. ON CONFLICT гарантирует
                # no-op при повторной постановке.
                await OutboxRepository.enqueue_idempotent(session, task.id, "update_lead")

                result.success = True
                result.user_message = "✏️ Задача возвращена исполнителю на доработку."
                result.task = task

                if assignee and assignee.id != user.id:
                    _build_rework_notifications(
                        notifications=result.notifications,
                        assignee_tg_id=assignee.tg_user_id,
                        requester=user,
                        task_id=task.id,
                        display_number=task.display_number,
                        task_title=task.title or "",
                        comment=comment,
                        files=rework_files,
                        attach_complete_kb=True,
                    )

        # ── ВНЕ session.begin(): commit прошёл, TG-side-effects ниже ──
        if result.success:
            await _apply_card_snapshot(bot, card_snap, task_id)

        # При возврате на доработку дедлайн остаётся прежним — переподнимаем
        # напоминания, которые были сняты в момент complete().
        if result.success and result.task is not None:
            try:
                schedule_reminders_for_task(
                    get_scheduler(),
                    task_id=result.task.id,
                    deadline_utc=result.task.deadline,
                )
            except RuntimeError as exc:
                logger.warning("rework reschedule reminders skipped: {}", exc)

        await _send_notifications(bot, result.notifications)
        if result.success:
            _try_update_lead_mirror(bot, task_id)
            _try_post_event_to_lead(
                bot,
                task_id,
                kind="rework",
                by_user_name=(user.full_name or _user_handle(user)),
                comment=comment,
                files=rework_files or [],
            )
        return result

    @staticmethod
    async def reassign(
        *,
        bot: Bot,
        task_id: int,
        new_assignee_id: int,
        user: User,
    ) -> ActionResult:
        """Передать активную задачу другому исполнителю того же отдела.

        Право: текущий assignee задачи или admin. Постановщик не может
        передать чужую задачу — это разруливается через cancel + новая
        задача. Право на reassign имеет именно тот, кто её взял.

        Условия:
          - статус ровно in_progress (на awaiting_approval не передаём —
            пусть постановщик согласует или вернёт);
          - new_assignee — активный approved-сотрудник того же department_id;
          - new_assignee != текущий assignee.

        Что делает:
          - меняет assignee_id, обновляет accepted_at = NOW;
          - пишет HistoryEventType.REASSIGNED с payload
            {from_user_id, to_user_id, by_role};
          - sync карточки в топике/зеркале;
          - новому исполнителю — DM-уведомление с inline-кнопкой «✅ Завершить»;
          - старому исполнителю — короткое DM «задача передана пользователю X»;
          - постановщику — короткое DM «задача передана от X к Y».
        """
        result = ActionResult(success=False, user_message="")
        card_snap: CardSyncSnap | None = None
        async with async_session_factory() as session:
            async with session.begin():
                task = await TasksRepository.lock_for_update(session, task_id)
                if task is None:
                    return ActionResult(False, "Задача не найдена.")

                # Право: текущий исполнитель или admin
                is_admin = user.role == UserRole.ADMIN.value
                if not is_admin and task.assignee_id != user.id:
                    return ActionResult(
                        False,
                        "Передать задачу может только её текущий исполнитель или админ.",
                    )

                if task.status != TaskStatus.IN_PROGRESS.value:
                    return ActionResult(
                        False,
                        "Передать можно только задачу в статусе «В работе».",
                    )

                if new_assignee_id == task.assignee_id:
                    return ActionResult(False, "Этот же исполнитель уже на задаче.")

                new_user = await UsersRepository.get_by_id(session, new_assignee_id)
                if (
                    new_user is None
                    or not new_user.is_active
                    or new_user.access_status != "approved"
                ):
                    return ActionResult(False, "Новый исполнитель не найден или не активен.")
                if new_user.department_id != task.department_id:
                    return ActionResult(
                        False, "Передавать задачу можно только сотруднику того же отдела."
                    )

                old_assignee_id = task.assignee_id
                task.assignee_id = new_user.id
                task.accepted_at = now_utc()
                await session.flush()

                by_role = "admin" if is_admin else "assignee"
                await HistoryRepository.log(
                    session,
                    task_id=task.id,
                    user_id=user.id,
                    event_type=HistoryEventType.REASSIGNED,
                    payload={
                        "from_user_id": old_assignee_id,
                        "to_user_id": new_user.id,
                        "by_role": by_role,
                    },
                )

                creator = await UsersRepository.get_by_id(session, task.creator_id)
                old_assignee = (
                    await UsersRepository.get_by_id(session, old_assignee_id)
                    if old_assignee_id
                    else None
                )
                department = await DepartmentsRepository.get_by_id(session, task.department_id)
                files_count = await TasksRepository.count_files(session, task.id)
                card_snap = _build_card_snapshot(
                    task,
                    creator=creator,
                    assignee=new_user,
                    department=department,
                    files_count=files_count,
                )

                # Атомарно ставим update_lead в outbox в той же транзакции,
                # что и смена статуса. Если процесс упадёт после commit'а
                # main txn, но до fire-and-forget create_task — периодический
                # outbox-воркер всё равно подхватит. ON CONFLICT гарантирует
                # no-op при повторной постановке.
                await OutboxRepository.enqueue_idempotent(session, task.id, "update_lead")

                result.success = True
                result.user_message = f"🔄 Задача передана {_user_handle(new_user)}."
                result.task = task

                # DM новому исполнителю — с кнопкой «✅ Завершить»
                disp = task.display_number
                title = (task.title or "").strip() or "(без названия)"
                if len(title) > 80:
                    title = title[:79] + "…"
                title = _esc(title)
                new_text = (
                    f"📥 Тебе передана задача #{disp} «{title}».\n"
                    f"От: {_user_handle(user)}.\n"
                    "Когда будет готов отчёт — нажми «✅ Завершить» ниже."
                )
                result.notifications.append(
                    {
                        "kind": "text",
                        "chat_id": new_user.tg_user_id,
                        "text": new_text,
                        "reply_markup": build_complete_action_kb(task.id),
                    }
                )

                # DM старому исполнителю (если есть и не он сам инициатор)
                if old_assignee and old_assignee.id != user.id:
                    result.notifications.append(
                        {
                            "kind": "text",
                            "chat_id": old_assignee.tg_user_id,
                            "text": (
                                f"🔄 Задача #{disp} «{title}» передана "
                                f"{_user_handle(new_user)}. С тебя снята."
                            ),
                        }
                    )

                # DM постановщику
                if creator and creator.id not in {user.id, new_user.id}:
                    result.notifications.append(
                        {
                            "kind": "text",
                            "chat_id": creator.tg_user_id,
                            "text": (
                                f"🔄 По задаче #{disp} «{title}» новый исполнитель: "
                                f"{_user_handle(new_user)} (передал "
                                f"{_user_handle(user)})."
                            ),
                        }
                    )

        # ── ВНЕ session.begin(): commit прошёл, TG-side-effects ниже ──
        if result.success:
            await _apply_card_snapshot(bot, card_snap, task_id)

        await _send_notifications(bot, result.notifications)
        if result.success:
            _try_update_lead_mirror(bot, task_id)
            _try_post_event_to_lead(
                bot,
                task_id,
                kind="reassign",
                by_user_name=(user.full_name or _user_handle(user)),
                comment=(
                    f"Передана {_user_handle(new_user)}"
                    if not old_assignee
                    else f"Передана {_user_handle(old_assignee)} → {_user_handle(new_user)}"
                ),
                files=[],
            )
        return result

    @staticmethod
    async def cancel(*, bot: Bot, task_id: int, user: User) -> ActionResult:
        """
        Отмена задачи. Право: только creator или admin.
        Допустимы переходы NEW→CANCELLED и IN_PROGRESS→CANCELLED.
        """
        result = ActionResult(success=False, user_message="")
        card_snap: CardSyncSnap | None = None
        async with async_session_factory() as session:
            async with session.begin():
                task = await TasksRepository.lock_for_update(session, task_id)
                if task is None:
                    return ActionResult(False, "Задача не найдена.")

                if not _can_cancel(task, user):
                    return ActionResult(
                        False, "Отменить задачу может только постановщик или админ."
                    )

                if task.status == TaskStatus.CANCELLED.value:
                    return ActionResult(False, "Задача уже отменена.")
                if task.status not in {
                    TaskStatus.NEW.value,
                    TaskStatus.IN_PROGRESS.value,
                    TaskStatus.AWAITING_APPROVAL.value,
                }:
                    return ActionResult(
                        False,
                        f"Задача в статусе «{task.status}» — отменить нельзя.",
                    )

                _assert_transition(task.status, TaskStatus.CANCELLED)
                task.status = TaskStatus.CANCELLED.value
                await session.flush()

                by_role = "admin" if user.role == UserRole.ADMIN.value else "creator"
                await HistoryRepository.log(
                    session,
                    task_id=task.id,
                    user_id=user.id,
                    event_type=HistoryEventType.CANCELLED,
                    payload={"by_role": by_role},
                )

                creator = await UsersRepository.get_by_id(session, task.creator_id)
                assignee = (
                    await UsersRepository.get_by_id(session, task.assignee_id)
                    if task.assignee_id
                    else None
                )
                department = await DepartmentsRepository.get_by_id(session, task.department_id)
                files_count = await TasksRepository.count_files(session, task.id)
                card_snap = _build_card_snapshot(
                    task,
                    creator=creator,
                    assignee=assignee,
                    department=department,
                    files_count=files_count,
                )

                # Атомарно ставим update_lead в outbox в той же транзакции,
                # что и смена статуса. Если процесс упадёт после commit'а
                # main txn, но до fire-and-forget create_task — периодический
                # outbox-воркер всё равно подхватит. ON CONFLICT гарантирует
                # no-op при повторной постановке.
                await OutboxRepository.enqueue_idempotent(session, task.id, "update_lead")

                result.success = True
                result.user_message = "❌ Задача отменена."
                result.task = task
                msg = f"❌ {_user_handle(user)} отменил(а) задачу #{task.display_number}."
                targets: set[int] = set()
                if creator and creator.id != user.id:
                    targets.add(creator.tg_user_id)
                if assignee and assignee.id != user.id:
                    targets.add(assignee.tg_user_id)
                for tg_id in targets:
                    result.notifications.append({"kind": "text", "chat_id": tg_id, "text": msg})

        # ── ВНЕ session.begin(): commit прошёл, TG-side-effects ниже ──
        if result.success:
            await _apply_card_snapshot(bot, card_snap, task_id)
            _drop_reminders(task_id)
        await _send_notifications(bot, result.notifications)
        if result.success:
            _try_update_lead_mirror(bot, task_id)
            _try_purge_overdue(bot, task_id)
        return result

    @staticmethod
    async def add_comment(
        *,
        bot: Bot,
        task_id: int,
        user: User,
        text: str,
    ) -> ActionResult:
        """
        Добавляет комментарий к задаче без смены статуса.
        Право: creator, assignee, admin, lead.
        """
        result = ActionResult(success=False, user_message="")
        async with async_session_factory() as session:
            async with session.begin():
                task = await TasksRepository.lock_for_update(session, task_id)
                if task is None:
                    return ActionResult(False, "Задача не найдена.")

                if not _can_comment(task, user):
                    return ActionResult(
                        False,
                        "Комментировать задачу может постановщик, исполнитель, "
                        "руководитель отдела или админ.",
                    )

                trimmed = text.strip()
                if not trimmed:
                    return ActionResult(False, "Комментарий пустой.")

                await HistoryRepository.log(
                    session,
                    task_id=task.id,
                    user_id=user.id,
                    event_type=HistoryEventType.COMMENTED,
                    payload={"text": trimmed, "from_user_role": user.role},
                )

                creator = await UsersRepository.get_by_id(session, task.creator_id)
                assignee = (
                    await UsersRepository.get_by_id(session, task.assignee_id)
                    if task.assignee_id
                    else None
                )

                result.success = True
                result.user_message = "💬 Комментарий добавлен."
                result.task = task

                msg = f"💬 {_user_handle(user)} к задаче #{task.display_number}:\n{trimmed}"
                targets: set[int] = set()
                if creator and creator.id != user.id:
                    targets.add(creator.tg_user_id)
                if assignee and assignee.id != user.id:
                    targets.add(assignee.tg_user_id)
                for tg_id in targets:
                    result.notifications.append({"kind": "text", "chat_id": tg_id, "text": msg})

        await _send_notifications(bot, result.notifications)
        return result


# ──────────────────────────────────────────────────────────────────────
# Permission predicates (вынесены, чтобы не размазывать логику)
# ──────────────────────────────────────────────────────────────────────


def _can_complete(task: Task, user: User) -> bool:
    if user.role == UserRole.ADMIN.value:
        return True
    return task.assignee_id == user.id


def _can_cancel(task: Task, user: User) -> bool:
    if user.role == UserRole.ADMIN.value:
        return True
    return task.creator_id == user.id


def _can_approve(task: Task, user: User) -> bool:
    """Согласовать / вернуть на доработку — ТОЛЬКО постановщик.

    Принципиально: «принимает работу» только тот, кто её ставил.
    Админ не подменяет постановщика — иначе любой админ может закрыть
    чужую задачу мимо реального заказчика. Если постановщик неактивен
    и задача висит — это разруливается отдельно (ручное вмешательство).
    """
    return task.creator_id == user.id


def _can_comment(task: Task, user: User) -> bool:
    if user.role in {UserRole.ADMIN.value, UserRole.LEAD.value}:
        return True
    return user.id in {task.creator_id, task.assignee_id or -1}


# ──────────────────────────────────────────────────────────────────────
# Notifications dispatch
# ──────────────────────────────────────────────────────────────────────


TG_CAPTION_MAX = 1024  # лимит Telegram на caption у медиа


def _build_completion_notifications(
    *,
    notifications: list[dict[str, Any]],
    creator_tg_id: int,
    executor: User,
    task_id: int,
    display_number: int,
    task_title: str,
    dept_name: str | None,
    comment: str | None,
    files: list[dict[str, Any]],
    attach_approval_kb: bool = False,
) -> None:
    """
    Формирует пакет DM постановщику при отправке отчёта на согласование.

    `attach_approval_kb=True` (Этап Б): к ПОСЛЕДНЕМУ сообщению в пакете
    прикрепляется inline-клавиатура [✅ Согласовано] [✏️ На доработку].
    Это даёт постановщику возможность принять решение прямо в DM,
    не открывая задачу отдельно.
    """
    title = (task_title or "").strip() or "(без названия)"
    if len(title) > 80:
        title = title[:79] + "…"
    title = _esc(title)

    header_lines = [
        f"📨 {_user_handle(executor)} отправил(а) отчёт по задаче #{display_number} "
        f"«{title}» на согласование."
    ]
    if dept_name:
        header_lines.append(f"📁 Отдел: {_esc(dept_name)}")
    if comment:
        header_lines.append(f"💬 {_esc(comment)}")
    if files:
        header_lines.append(f"📎 Вложений: {len(files)}")
    # Если исполнитель ничего не приложил — явно об этом сообщаем.
    # Без этой строки постановщик видит «отправил отчёт» с пустым телом и
    # ошибочно считает что отчёт потерялся (а это намеренно пустой отчёт).
    if not comment and not files:
        header_lines.append("📭 Без приложений и комментария.")
    header = "\n".join(header_lines)
    kb = build_approval_kb(task_id) if attach_approval_kb else None

    # Если ровно один файл — отправим его сразу с caption-заголовком,
    # без дублирующего текстового сообщения. Это экономит экран и
    # делает ленту в личке компактнее.
    if len(files) == 1:
        f = files[0]
        caption = header if len(header) <= TG_CAPTION_MAX else header[: TG_CAPTION_MAX - 1] + "…"
        notifications.append(
            {
                "kind": f["kind"],
                "chat_id": creator_tg_id,
                "file_id": f["file_id"],
                "file_name": f.get("file_name"),
                "caption": caption,
                "reply_markup": kb,
            }
        )
        return

    # Несколько файлов или ноль — сначала текстовый заголовок, потом по
    # одному файлу без caption (caption у каждого был бы шумом).
    # Клавиатуру согласования вешаем на ПОСЛЕДНЕЕ сообщение, чтобы кнопки
    # были видны в конце ленты, ниже всех файлов.
    if not files:
        notifications.append(
            {
                "kind": "text",
                "chat_id": creator_tg_id,
                "text": header,
                "reply_markup": kb,
            }
        )
        return

    notifications.append({"kind": "text", "chat_id": creator_tg_id, "text": header})
    for idx, f in enumerate(files):
        is_last = idx == len(files) - 1
        notifications.append(
            {
                "kind": f["kind"],
                "chat_id": creator_tg_id,
                "file_id": f["file_id"],
                "file_name": f.get("file_name"),
                "caption": None,
                "reply_markup": kb if is_last else None,
            }
        )


def _build_rework_notifications(
    *,
    notifications: list[dict[str, Any]],
    assignee_tg_id: int,
    requester: User,
    task_id: int,
    display_number: int,
    task_title: str,
    comment: str | None,
    files: list[dict[str, Any]],
    attach_complete_kb: bool = False,
) -> None:
    """
    Уведомление исполнителю при возврате задачи на доработку.

    В Этапе Б `comment` и `files` опциональны (постановщик может вернуть
    одной кнопкой). В Этапе В comment обязателен — это будет проверено
    в FSM хэндлере; здесь же мы просто рисуем то, что пришло.

    `attach_complete_kb=True` — прикрепляет к ПОСЛЕДНЕМУ сообщению пакета
    inline-кнопку «✅ Завершить» (build_complete_action_kb). Симметрия с
    attach_approval_kb для постановщика: иначе исполнитель видит правки,
    но не имеет кнопки следующего шага в DM — а карточка в топике отдела
    может быть недоступна (БД-only режим / нет dept_message_id).
    """
    title = (task_title or "").strip() or "(без названия)"
    if len(title) > 80:
        title = title[:79] + "…"
    title = _esc(title)

    header_lines = [
        f"✏️ {_user_handle(requester)} вернул(а) задачу #{display_number} «{title}» на доработку."
    ]
    if comment:
        header_lines.append(f"💬 Что поправить: {_esc(comment)}")
    if files:
        header_lines.append(f"📎 Прикреплено материалов: {len(files)}")
    header_lines.append(
        "Задача снова у тебя в работе.\n"
        "Когда внесёшь правки, нажми «✅ Завершить» внизу этого сообщения. "
        "Бот откроет отчёт, и тогда ты приложишь готовый файл и пояснение."
    )
    header = "\n".join(header_lines)
    kb = build_complete_action_kb(task_id) if attach_complete_kb else None

    if len(files) == 1:
        f = files[0]
        caption = header if len(header) <= TG_CAPTION_MAX else header[: TG_CAPTION_MAX - 1] + "…"
        notifications.append(
            {
                "kind": f["kind"],
                "chat_id": assignee_tg_id,
                "file_id": f["file_id"],
                "file_name": f.get("file_name"),
                "caption": caption,
                "reply_markup": kb,
            }
        )
        return

    if not files:
        notifications.append(
            {
                "kind": "text",
                "chat_id": assignee_tg_id,
                "text": header,
                "reply_markup": kb,
            }
        )
        return

    notifications.append({"kind": "text", "chat_id": assignee_tg_id, "text": header})
    for idx, f in enumerate(files):
        is_last = idx == len(files) - 1
        notifications.append(
            {
                "kind": f["kind"],
                "chat_id": assignee_tg_id,
                "file_id": f["file_id"],
                "file_name": f.get("file_name"),
                "caption": None,
                "reply_markup": kb if is_last else None,
            }
        )


async def _send_notifications(bot: Bot, notifications: list[dict[str, Any]]) -> None:
    """
    Шлёт пакет уведомлений по одному, best-effort.

    Файлы → send_file_with_retry (с диспетчеризацией по kind, с retry на flood).
    Текст → send_dm_safe (best-effort, не падает на Forbidden).
    Любая ошибка одного итема не блокирует остальные.
    """
    for n in notifications:
        kind = n.get("kind", "text")
        chat_id = n["chat_id"]
        reply_markup = n.get("reply_markup")
        try:
            if kind == "text":
                await send_dm_safe(bot, chat_id=chat_id, text=n["text"], reply_markup=reply_markup)
            else:
                await send_file_with_retry(
                    bot,
                    chat_id=chat_id,
                    file_id=n["file_id"],
                    kind=kind,
                    file_name=n.get("file_name"),
                    caption=n.get("caption"),
                    reply_markup=reply_markup,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — best-effort
            logger.warning(
                "notification dispatch failed (kind={}, chat={}): {}",
                kind,
                chat_id,
                exc,
            )


def _drop_reminders(task_id: int) -> None:
    """Снимает все reminder-job'ы задачи. Безопасно при отсутствии scheduler."""
    try:
        remove_reminders_for_task(get_scheduler(), task_id)
    except RuntimeError as exc:
        logger.warning("remove_reminders_for_task skipped: {}", exc)
