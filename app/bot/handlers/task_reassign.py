"""
Передача задачи другому исполнителю того же отдела.

Поток:
  1. В карточке задачи (status=in_progress) текущий assignee жмёт
     «🔄 Передать другому» → cb_reassign_start.
  2. Бот показывает inline-список approved-сотрудников этого же отдела
     (без текущего исполнителя). Если список пуст — alert.
  3. Юзер выбирает кандидата → cb_reassign_pick → бот спрашивает
     подтверждение (двухшаговое, чтобы не пере-передавать случайно).
  4. «✅ Подтвердить» → TaskActionsService.reassign(task_id, new_id, user).
     Сервис сам делает _sync_card, шлёт DM, обновляет Руководство.
  5. «⬅️ Отмена» — возврат к карточке (kb действий по статусу).
"""

import asyncio

from aiogram import Bot, F, Router
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from loguru import logger

from app.bot.keyboards.task_card import build_task_card_kb
from app.db.base import async_session_factory
from app.db.enums import UserRole
from app.db.exceptions import InvalidStatusTransition
from app.db.models import User
from app.db.repositories.tasks import TasksRepository
from app.db.repositories.users import UsersRepository
from app.services.task_actions import TaskActionsService

router = Router(name="task_reassign")


# ──────────────────────────────────────────────────────────────────────
# helpers
# ──────────────────────────────────────────────────────────────────────


def _parse_task_id(data: str | None) -> int | None:
    """task_reassign:{id} → id"""
    if not data:
        return None
    parts = data.split(":")
    if len(parts) < 2:
        return None
    try:
        return int(parts[1])
    except ValueError:
        return None


def _parse_pick(data: str | None) -> tuple[int, int] | None:
    """treass_to:{task_id}:{new_user_id} → (task_id, new_user_id)"""
    if not data:
        return None
    parts = data.split(":")
    if len(parts) != 3:
        return None
    try:
        return int(parts[1]), int(parts[2])
    except ValueError:
        return None


def _parse_confirm(data: str | None) -> tuple[int, int] | None:
    """treass_go:{task_id}:{new_user_id} → (task_id, new_user_id)"""
    return _parse_pick(data)


async def _ensure_user(cq: CallbackQuery, user: User | None) -> User | None:
    if user is None:
        async with async_session_factory() as s:
            user = await UsersRepository.get_by_tg_id(s, cq.from_user.id)
    if user is None or not user.is_active:
        await cq.answer("⏳ Аккаунт не активирован.", show_alert=True)
        return None
    return user


def _user_label(u: User) -> str:
    name = u.full_name or f"id={u.tg_user_id}"
    if u.tg_username:
        return f"{name} (@{u.tg_username})"
    return name


def _build_assignee_picker_kb(
    task_id: int,
    candidates: list[User],
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for u in candidates:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"👤 {_user_label(u)}",
                    callback_data=f"treass_to:{task_id}:{u.id}",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="⬅️ Отмена", callback_data=f"treass_back:{task_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _build_confirm_kb(task_id: int, new_user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Подтвердить передачу",
                    callback_data=f"treass_go:{task_id}:{new_user_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Назад к списку",
                    callback_data=f"task_reassign:{task_id}",
                )
            ],
        ]
    )


# ──────────────────────────────────────────────────────────────────────
# handlers
# ──────────────────────────────────────────────────────────────────────


@router.callback_query(F.data.startswith("task_reassign:"))
async def cb_reassign_start(cq: CallbackQuery, user: User | None = None) -> None:
    """Показывает список кандидатов из того же отдела."""
    user = await _ensure_user(cq, user)
    if user is None:
        return
    task_id = _parse_task_id(cq.data)
    if task_id is None:
        await cq.answer("Некорректная задача.", show_alert=True)
        return

    async with async_session_factory() as session:
        task = await TasksRepository.get_by_id(session, task_id)
        if task is None:
            await cq.answer("Задача не найдена.", show_alert=True)
            return
        # право: текущий assignee или admin
        is_admin = user.role == UserRole.ADMIN.value
        if not is_admin and task.assignee_id != user.id:
            await cq.answer(
                "Передать может только текущий исполнитель или админ.",
                show_alert=True,
            )
            return
        if task.status != "in_progress":
            await cq.answer(
                "Передать можно только задачу в работе.",
                show_alert=True,
            )
            return
        candidates = await UsersRepository.list_approved_in_department(
            session,
            department_id=task.department_id,
            exclude_user_id=task.assignee_id,
        )

    if not candidates:
        await cq.answer(
            "В отделе нет других активных сотрудников, кому можно передать.",
            show_alert=True,
        )
        return

    kb = _build_assignee_picker_kb(task_id, list(candidates))
    text = f"🔄 <b>Передать задачу #{task.display_number}</b>\nВыбери нового исполнителя из отдела:"
    if isinstance(cq.message, Message):
        try:
            await cq.message.edit_text(text, reply_markup=kb)
        except Exception:  # noqa: BLE001
            await cq.message.answer(text, reply_markup=kb)
    await cq.answer()


@router.callback_query(F.data.startswith("treass_to:"))
async def cb_reassign_pick(cq: CallbackQuery, user: User | None = None) -> None:
    """Юзер выбрал кандидата — показываем подтверждение."""
    user = await _ensure_user(cq, user)
    if user is None:
        return
    parsed = _parse_pick(cq.data)
    if parsed is None:
        await cq.answer("Некорректные данные.", show_alert=True)
        return
    task_id, new_user_id = parsed

    async with async_session_factory() as session:
        new_user = await UsersRepository.get_by_id(session, new_user_id)
        task = await TasksRepository.get_by_id(session, task_id)
    if new_user is None or task is None:
        await cq.answer("Данные устарели, открой заново.", show_alert=True)
        return

    text = (
        f"🔄 <b>Подтверди передачу задачи #{task.display_number}</b>\n\n"
        f"Новый исполнитель: <b>{_user_label(new_user)}</b>\n\n"
        "Подтверди — задача станет его, тебе она больше не будет видна "
        "в личных. Если передумал — нажми «Назад к списку»."
    )
    kb = _build_confirm_kb(task_id, new_user_id)
    if isinstance(cq.message, Message):
        try:
            await cq.message.edit_text(text, reply_markup=kb)
        except Exception:  # noqa: BLE001
            await cq.message.answer(text, reply_markup=kb)
    await cq.answer()


@router.callback_query(F.data.startswith("treass_back:"))
async def cb_reassign_back(cq: CallbackQuery, user: User | None = None) -> None:
    """Возврат к карточке: показываем нативную кнопочную клавиатуру по статусу."""
    user = await _ensure_user(cq, user)
    if user is None:
        return
    task_id = _parse_task_id(cq.data)
    if task_id is None:
        await cq.answer()
        return
    async with async_session_factory() as session:
        task = await TasksRepository.get_by_id(session, task_id)
    if task is None:
        await cq.answer("Задача не найдена.", show_alert=True)
        return
    kb = build_task_card_kb(task.status, task.id)
    if isinstance(cq.message, Message):
        try:
            await cq.message.edit_reply_markup(reply_markup=kb)
        except Exception as exc:  # noqa: BLE001
            logger.debug("treass_back edit_reply_markup failed: {}", exc)
    await cq.answer("Передача отменена.")


@router.callback_query(F.data.startswith("treass_go:"))
async def cb_reassign_confirm(
    cq: CallbackQuery,
    bot: Bot,
    user: User | None = None,
) -> None:
    """Финальное подтверждение — вызываем сервис."""
    user = await _ensure_user(cq, user)
    if user is None:
        return
    parsed = _parse_confirm(cq.data)
    if parsed is None:
        await cq.answer("Некорректные данные.", show_alert=True)
        return
    task_id, new_user_id = parsed

    try:
        result = await TaskActionsService.reassign(
            bot=bot,
            task_id=task_id,
            new_assignee_id=new_user_id,
            user=user,
        )
    except InvalidStatusTransition:
        await cq.answer(
            "Действие уже невозможно — статус задачи изменился.",
            show_alert=True,
        )
        return
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("task_reassign failed for #{}: {}", task_id, exc)
        await cq.answer("Ошибка. Попробуй ещё раз позже.", show_alert=True)
        return

    await cq.answer(result.user_message, show_alert=not result.success)
    if result.success and isinstance(cq.message, Message):
        disp = result.task.display_number if result.task is not None else task_id
        try:
            await cq.message.edit_text(f"🔄 Задача #{disp} передана. Уведомления отправлены.")
        except Exception:  # noqa: BLE001
            pass
