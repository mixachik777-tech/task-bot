"""
Меню «👥 Состав» — кто в каком отделе + распределение по отделам.

UX:
- Все активные сотрудники видят раздел как список с указанием отдела.
- Admin дополнительно может тапнуть на любого сотрудника → подменю
  «переместить в отдел / убрать из отдела». После перемещения список
  обновляется на месте (edit_text).

callback_data:
  tm:u:{user_id}              — открыть подменю перемещения
  tm:m:{user_id}:{dept_id|0}  — переместить (0 = убрать из отдела)
  tm:back                     — вернуться к общему списку
"""

from __future__ import annotations

import html
from collections import OrderedDict

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from loguru import logger

from app.bot.keyboards.main_menu import BTN_TEAM
from app.bot.runtime import get_bot, get_redis
from app.bot.utils.access import is_telegram_admin
from app.db.base import async_session_factory
from app.db.enums import UserRole
from app.db.models import User
from app.db.repositories.app_settings import KEY_TASK_CHAT_ID, AppSettingsRepository
from app.db.repositories.departments import DepartmentsRepository
from app.db.repositories.tasks import TasksRepository
from app.db.repositories.users import UsersRepository

router = Router(name="team")


class TeamBrowse(StatesGroup):
    viewing = State()


ROLE_LABEL: dict[str, str] = {
    UserRole.EMPLOYEE.value: "",
    UserRole.LEAD.value: " · 👑 руководитель",
    UserRole.ADMIN.value: " · 🛡 админ",
}


def _user_short(u: User) -> str:
    name = html.escape(u.full_name or f"id={u.tg_user_id}")
    handle = f" (@{html.escape(u.tg_username)})" if u.tg_username else ""
    return name + handle + ROLE_LABEL.get(u.role, "")


def _user_button_label(u: User) -> str:
    """Короткая подпись для inline-кнопки в admin-режиме."""
    name = u.full_name or (f"@{u.tg_username}" if u.tg_username else f"#{u.id}")
    dept = u.department.name if getattr(u, "department", None) else "—"
    label = f"{name} · {dept}"
    if len(label) > 50:
        label = label[:47] + "…"
    return label


def _render_summary(users: list[User], depts: list) -> str:
    """Текстовая сводка: «Дизайнеры: 2 · Корректоры: 1 · Без отдела: 0»."""
    by_dept_count: dict[int, int] = {}
    no_dept = 0
    for u in users:
        if u.department_id is None:
            no_dept += 1
        else:
            by_dept_count[u.department_id] = by_dept_count.get(u.department_id, 0) + 1
    chips = [f"🏢 {d.name}: {by_dept_count.get(d.id, 0)}" for d in depts]
    chips.append(f"Без отдела: {no_dept}")
    return " · ".join(chips)


def _render_text_list(users: list[User]) -> str:
    """
    Для не-admin: плоский текст с группировкой по отделам, как было в
    предыдущей версии «Контакты», но с заголовком «Состав».
    """
    if not users:
        return "<b>👥 Состав</b>\n\nПока пусто."
    by_dept: "OrderedDict[str, list[User]]" = OrderedDict()
    no_dept: list[User] = []
    for u in users:
        if u.department is None:
            no_dept.append(u)
        else:
            by_dept.setdefault(u.department.name, []).append(u)

    sections: list[str] = ["<b>👥 Состав</b>"]
    if not by_dept:
        sections.append("")
        for u in no_dept:
            sections.append("• " + _user_short(u))
        return "\n".join(sections)

    for dept_name, group in by_dept.items():
        sections.append("")
        sections.append(f"<b>🏢 {html.escape(dept_name)}</b>")
        for u in group:
            sections.append("• " + _user_short(u))
    if no_dept:
        sections.append("")
        sections.append("<b>🏢 Без отдела</b>")
        for u in no_dept:
            sections.append("• " + _user_short(u))
    return "\n".join(sections)


def _kb_admin_list(users: list[User]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for u in users:
        rows.append(
            [
                InlineKeyboardButton(
                    text=_user_button_label(u),
                    callback_data=f"tm:u:{u.id}",
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _kb_move_user(user_id: int, depts: list, current_dept_id: int | None) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for d in depts:
        mark = "✅ " if d.id == current_dept_id else ""
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{mark}🏢 {d.name}",
                    callback_data=f"tm:m:{user_id}:{d.id}",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text=("✅ " if current_dept_id is None else "")
                + "➖ Убрать из отдела",
                callback_data=f"tm:m:{user_id}:0",
            )
        ]
    )
    rows.append(
        [InlineKeyboardButton(text="⬅️ Назад к составу", callback_data="tm:back")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _load_users_and_depts() -> tuple[list[User], list]:
    async with async_session_factory() as session:
        async with session.begin():
            users = list(await UsersRepository.list_active_with_department(session))
            depts = list(await DepartmentsRepository.list_active(session))
    return users, depts


async def _is_chat_admin(user: User) -> bool:
    """
    Управление составом разрешено только реальным админам Telegram-чата
    (creator/administrator супергруппы TASK_CHAT_ID), а не просто
    `role=admin` в БД. Без TASK_CHAT_ID — никому: некому проверять.
    """
    async with async_session_factory() as session:
        async with session.begin():
            chat_id = await AppSettingsRepository.get_int(session, KEY_TASK_CHAT_ID)
    if not chat_id:
        return False
    return await is_telegram_admin(
        get_bot(), get_redis(), chat_id, user.tg_user_id
    )


async def _send_team_view(
    target_message: Message,
    user: User,
    *,
    edit: bool,
    can_manage: bool,
) -> None:
    users, depts = await _load_users_and_depts()
    if can_manage:
        summary = _render_summary(users, depts)
        body = (
            f"<b>👥 Состав</b>\n{summary}\n\n"
            "Тапни на сотрудника, чтобы перенести его в отдел."
        )
        kb: InlineKeyboardMarkup | None = _kb_admin_list(users) if users else None
    else:
        body = _render_text_list(users)
        kb = None
    if edit:
        try:
            await target_message.edit_text(body, reply_markup=kb)
            return
        except Exception as exc:  # noqa: BLE001
            logger.debug("team: edit_text failed, fallback to answer: {}", exc)
    await target_message.answer(body, reply_markup=kb)


@router.message(F.chat.type == "private", F.text == BTN_TEAM)
async def msg_team_open(message: Message, state: FSMContext, user: User) -> None:
    if not user.is_active:
        await message.answer("⏳ Аккаунт ожидает одобрения администратором.")
        return
    await state.clear()
    can_manage = await _is_chat_admin(user)
    if can_manage:
        await state.set_state(TeamBrowse.viewing)
    await _send_team_view(message, user, edit=False, can_manage=can_manage)


@router.callback_query(StateFilter(TeamBrowse.viewing), F.data == "tm:back")
async def cb_back(cq: CallbackQuery, state: FSMContext, user: User) -> None:
    if cq.message is None:
        await cq.answer()
        return
    can_manage = await _is_chat_admin(user)
    if not can_manage:
        await cq.answer("Управление доступно только админам чата", show_alert=True)
        await state.clear()
        return
    await _send_team_view(cq.message, user, edit=True, can_manage=True)
    await cq.answer()


@router.callback_query(StateFilter(TeamBrowse.viewing), F.data.startswith("tm:u:"))
async def cb_user_open(cq: CallbackQuery, state: FSMContext, user: User) -> None:
    if not await _is_chat_admin(user):
        await cq.answer("Только админы чата могут перемещать сотрудников", show_alert=True)
        return
    target_id = int(cq.data.split(":")[2])
    async with async_session_factory() as session:
        async with session.begin():
            target = await UsersRepository.get_by_id(session, target_id)
            depts = list(await DepartmentsRepository.list_active(session))
    if target is None:
        await cq.answer("Сотрудник не найден", show_alert=True)
        return
    current_dept_name = "—"
    if target.department_id:
        for d in depts:
            if d.id == target.department_id:
                current_dept_name = d.name
                break
    name = html.escape(target.full_name or f"id={target.tg_user_id}")
    body = (
        f"<b>👤 {name}</b>\n"
        f"Сейчас: <b>{html.escape(current_dept_name)}</b>\n\n"
        "Переместить в отдел:"
    )
    kb = _kb_move_user(target.id, depts, target.department_id)
    if cq.message is not None:
        try:
            await cq.message.edit_text(body, reply_markup=kb)
        except Exception as exc:  # noqa: BLE001
            logger.warning("team: open user-menu edit failed: {}", exc)
    await cq.answer()


@router.callback_query(StateFilter(TeamBrowse.viewing), F.data.startswith("tm:m:"))
async def cb_move(cq: CallbackQuery, state: FSMContext, user: User) -> None:
    if not await _is_chat_admin(user):
        await cq.answer("Только админы чата могут перемещать сотрудников", show_alert=True)
        return
    parts = cq.data.split(":")
    target_id = int(parts[2])
    raw_dept = int(parts[3])
    new_dept_id: int | None = None if raw_dept == 0 else raw_dept

    open_tasks_count = 0
    async with async_session_factory() as session:
        async with session.begin():
            target = await UsersRepository.get_by_id(session, target_id)
            if target is None:
                await cq.answer("Сотрудник не найден", show_alert=True)
                return
            if new_dept_id is not None:
                dept = await DepartmentsRepository.get_by_id(session, new_dept_id)
                if dept is None or not dept.is_active:
                    await cq.answer("Отдел не найден", show_alert=True)
                    return
            else:
                # Уход из отдела — считаем открытые задачи, чтобы предупредить
                # админа: assignee не обнуляем, но эти задачи теперь висят на
                # сотруднике без отдела (в фильтре «по отделу» они не видны).
                open_tasks = await TasksRepository.list_active(
                    session, assignee_id=target.id
                )
                open_tasks_count = len(open_tasks)
            await UsersRepository.update_department(
                session, user_id=target.id, department_id=new_dept_id
            )

    if cq.message is not None:
        await _send_team_view(cq.message, user, edit=True, can_manage=True)
    if new_dept_id:
        await cq.answer("Перенесён в отдел")
    elif open_tasks_count > 0:
        await cq.answer(
            f"Убран из отдела. У него осталось открытых задач: "
            f"{open_tasks_count} — переназначь или закрой их.",
            show_alert=True,
        )
    else:
        await cq.answer("Убран из отдела")
