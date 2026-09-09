"""
Раздел «📋 Активные у сотрудников» в Кабинете админа.

Не путать с «📑 Все задачи» (all_tasks_admin) — там лента всех задач
включая завершённые. Здесь только активные (new / in_progress /
awaiting_approval) с группировкой по исполнителю.

Точка входа — inline-кнопка в Кабинете (cabinet.py добавляет её только
для admin). Доступ ограничен ролью admin.

callback_data:
  wl:list                 — обзор: сотрудник → N активных
  wl:u:{assignee_id}      — drill-down: задачи конкретного исполнителя
  wl:close                — закрыть раздел
"""

from __future__ import annotations

import html

from aiogram import F, Router
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from loguru import logger

from app.bot.utils.render import STATUS_EMOJI, STATUS_NAME
from app.bot.utils.tg import build_topic_message_link
from app.bot.utils.time import format_dt_local
from app.db.base import async_session_factory
from app.db.enums import UserRole
from app.db.models import Task, User
from app.db.repositories.app_settings import (
    KEY_LEADERSHIP_TOPIC_ID,
    KEY_TASK_CHAT_ID,
    AppSettingsRepository,
)
from app.db.repositories.tasks import TasksRepository
from app.db.repositories.users import UsersRepository

router = Router(name="admin_workload")

MAX_TASKS_PER_USER = 30


def _is_admin(user: User | None) -> bool:
    return user is not None and user.role == UserRole.ADMIN.value and user.is_active


def _short_handle(user: User | None) -> str:
    if user is None:
        return "—"
    if user.tg_username:
        return f"@{html.escape(user.tg_username)}"
    return html.escape(user.full_name or f"id={user.tg_user_id}")


def _user_label(user: User) -> str:
    name = user.full_name or (f"@{user.tg_username}" if user.tg_username else f"#{user.id}")
    return html.escape(name)


async def _render_overview() -> tuple[str, InlineKeyboardMarkup]:
    """Главная страница: список сотрудников и количество активных задач."""
    async with async_session_factory() as session:
        async with session.begin():
            summary = await TasksRepository.workload_summary(session)
            users_by_id: dict[int, User] = {}
            if summary:
                user_ids = [uid for uid, _ in summary]
                users = await UsersRepository.list_by_ids(session, user_ids)
                users_by_id = {u.id: u for u in users}

    if not summary:
        text = (
            "📋 <b>Активные у сотрудников</b>\n\n"
            "Сейчас ни у кого нет задач в работе. "
            "Завершённые задачи — в разделе «📁 Архив»."
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="✖️ Закрыть", callback_data="wl:close")]]
        )
        return text, kb

    total = sum(cnt for _, cnt in summary)
    lines = [
        "📋 <b>Активные у сотрудников</b>",
        f"Всего активных задач: {total}, исполнителей: {len(summary)}.",
        "Тапни сотрудника, чтобы посмотреть его текущие задачи.",
    ]
    rows: list[list[InlineKeyboardButton]] = []
    for uid, cnt in summary:
        u = users_by_id.get(uid)
        label = _user_label(u) if u is not None else f"#{uid}"
        if len(label) > 35:
            label = label[:34] + "…"
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{label} · {cnt}",
                    callback_data=f"wl:u:{uid}",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="✖️ Закрыть", callback_data="wl:close")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


def _render_task_line(task: Task) -> str:
    title = (task.title or "").strip() or "(без названия)"
    if len(title) > 60:
        title = title[:59] + "…"
    emoji = STATUS_EMOJI.get(task.status, "•")
    status_name = STATUS_NAME.get(task.status, task.status)
    parts = [
        f"<b>#{task.display_number}</b> «{html.escape(title)}»  [{emoji} {status_name}]",
        f"От: {_short_handle(task.creator)}",
    ]
    if task.department is not None:
        parts.append(f"Отдел: {html.escape(task.department.name)}")
    if task.deadline is not None:
        parts.append(f"Дедлайн: {format_dt_local(task.deadline)}")
    if task.accepted_at is not None:
        parts.append(f"Принята: {format_dt_local(task.accepted_at)}")
    return "\n".join(parts)


async def _render_user_page(assignee_id: int) -> tuple[str, InlineKeyboardMarkup]:
    async with async_session_factory() as session:
        async with session.begin():
            assignee = await UsersRepository.get_by_id(session, assignee_id)
            if assignee is None:
                text = "Сотрудник не найден."
                kb = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="⬅️ Назад", callback_data="wl:list")]
                    ]
                )
                return text, kb
            tasks = list(
                await TasksRepository.list_active_assigned_to(
                    session, assignee_id=assignee_id, limit=MAX_TASKS_PER_USER
                )
            )
            task_chat_id = (await AppSettingsRepository.get_int(session, KEY_TASK_CHAT_ID)) or 0
            lead_topic_id = (
                await AppSettingsRepository.get_int(session, KEY_LEADERSHIP_TOPIC_ID)
            ) or 0

    label = _user_label(assignee)
    if not tasks:
        text = f"📋 <b>{label}</b>\n\nСейчас активных задач нет."
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ Назад", callback_data="wl:list")],
                [InlineKeyboardButton(text="✖️ Закрыть", callback_data="wl:close")],
            ]
        )
        return text, kb

    header = f"📋 <b>{label}</b> — активных: {len(tasks)}"
    blocks = [header] + [_render_task_line(t) for t in tasks]
    text = "\n\n".join(blocks)

    rows: list[list[InlineKeyboardButton]] = []
    for t in tasks:
        title = (t.title or "").strip() or f"#{t.display_number}"
        if len(title) > 32:
            title = title[:31] + "…"
        task_row: list[InlineKeyboardButton] = []
        if t.arch_message_id and task_chat_id and lead_topic_id:
            link = build_topic_message_link(task_chat_id, lead_topic_id, t.arch_message_id)
            if link:
                task_row.append(InlineKeyboardButton(text=f"📂 {title}", url=link))
        task_row.append(
            InlineKeyboardButton(
                text=f"❌ Снять #{t.display_number}",
                callback_data=f"tcancel_ask:{t.id}",
            )
        )
        rows.append(task_row)
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="wl:list")])
    rows.append([InlineKeyboardButton(text="✖️ Закрыть", callback_data="wl:close")])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "wl:list")
async def cb_workload_list(cq: CallbackQuery, user: User | None = None) -> None:
    if not _is_admin(user):
        await cq.answer("Раздел только для админа.", show_alert=True)
        return
    text, kb = await _render_overview()
    if cq.message is not None:
        try:
            await cq.message.edit_text(text, reply_markup=kb)
        except Exception as exc:  # noqa: BLE001
            logger.debug("workload list edit failed: {}", exc)
            try:
                await cq.message.answer(text, reply_markup=kb)
            except Exception as exc2:  # noqa: BLE001
                logger.debug("workload list answer failed: {}", exc2)
    await cq.answer()


@router.callback_query(F.data.startswith("wl:u:"))
async def cb_workload_user(cq: CallbackQuery, user: User | None = None) -> None:
    if not _is_admin(user):
        await cq.answer("Раздел только для админа.", show_alert=True)
        return
    try:
        assignee_id = int((cq.data or "").split(":")[2])
    except (ValueError, IndexError):
        await cq.answer("Некорректный сотрудник.", show_alert=True)
        return
    text, kb = await _render_user_page(assignee_id)
    if cq.message is not None:
        try:
            await cq.message.edit_text(text, reply_markup=kb)
        except Exception as exc:  # noqa: BLE001
            logger.debug("workload user edit failed: {}", exc)
    await cq.answer()


@router.callback_query(F.data == "wl:close")
async def cb_workload_close(cq: CallbackQuery) -> None:
    if cq.message is not None:
        try:
            await cq.message.edit_text("Раздел «Активные у сотрудников» закрыт.")
        except Exception as exc:  # noqa: BLE001
            logger.debug("workload close edit failed: {}", exc)
    await cq.answer("Закрыто")
