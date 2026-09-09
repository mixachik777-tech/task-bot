"""
Меню «📁 Архив» — просмотр завершённых и отменённых задач.

Поток:
- Кнопка «📁 Архив» из главного меню (только в ЛС) переводит юзера в FSM
  `ArchiveBrowse.viewing` и присылает inline-список (1 страница = 10 строк)
  с навигацией и быстрыми фильтрами.
- Каждая строка списка — отдельная кнопка с коротким превью «#id ДД.ММ ЧЧ:ММ
  ✅ Заголовок». Клик открывает детальную карточку (render_task_card +
  render_task_history). «⬅️ К списку» возвращает.
- Меню фильтров: отдел / период / статус / исполнитель. Для employee
  фильтр по отделу зафиксирован на его собственном отделе и в UI не виден.
- Состояние фильтров и текущая страница хранятся в FSMContext.data,
  ключ — (chat_id, user_id). Это переживает рестарт бота (Redis).

callback_data:
  ar:l:{page}      — список, страница page (0-based)
  ar:fmenu         — открыть меню фильтров
  ar:fdept:{id|0}  — фильтр по отделу (0 = снять)
  ar:fstat:{s}     — фильтр по статусу: all | done | cancelled
  ar:fper:{p}      — фильтр по периоду: all | today | week | month
  ar:fassmenu      — открыть подменю выбора исполнителя
  ar:fass:{id|0}   — выбрать исполнителя
  ar:t:{id}        — детальная карточка задачи
  ar:close         — выйти из архива
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

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

from app.bot.keyboards.main_menu import BTN_ARCHIVE
from app.bot.utils.render import (
    render_archive_row,
    render_task_card,
    render_task_history,
)
from app.bot.utils.time import format_dt_local
from app.db.base import async_session_factory
from app.db.enums import UserRole
from app.db.repositories.analytics import period_bounds_utc
from app.db.repositories.app_settings import (
    KEY_LEADERSHIP_TOPIC_ID,
    KEY_TASK_CHAT_ID,
    AppSettingsRepository,
)
from app.db.repositories.departments import DepartmentsRepository
from app.db.repositories.history import HistoryRepository
from app.db.repositories.tasks import TasksRepository
from app.db.repositories.users import UsersRepository

router = Router(name="archive")

PAGE_SIZE = 10


class ArchiveBrowse(StatesGroup):
    viewing = State()


# ---------- состояние фильтров в FSMContext.data ----------

DATA_DEPT = "ar_dept"
DATA_ASSIGNEE = "ar_assignee"
DATA_STATUS = "ar_status"
DATA_PERIOD = "ar_period"
DATA_PAGE = "ar_page"


@dataclass(frozen=True)
class ArchiveFilters:
    department_id: int | None
    assignee_id: int | None
    status: str | None  # None=all, иначе 'done'/'cancelled'
    period: str | None  # None=all, иначе 'today'/'week'/'month'
    page: int


def _filters_from_data(data: dict) -> ArchiveFilters:
    return ArchiveFilters(
        department_id=data.get(DATA_DEPT),
        assignee_id=data.get(DATA_ASSIGNEE),
        status=data.get(DATA_STATUS),
        period=data.get(DATA_PERIOD),
        page=int(data.get(DATA_PAGE, 0)),
    )


def _date_bounds(period: str | None) -> tuple[datetime | None, datetime | None]:
    if period is None:
        return None, None
    return period_bounds_utc(period)


# ---------- клавиатуры ----------


def _kb_list(
    flt: ArchiveFilters,
    rows: list[tuple[int, str]],
    total: int,
    *,
    can_filter_dept: bool,
) -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []
    for task_id, label in rows:
        buttons.append([InlineKeyboardButton(text=label, callback_data=f"ar:t:{task_id}")])

    # пагинация
    last_page = max(0, (total - 1) // PAGE_SIZE)
    nav: list[InlineKeyboardButton] = []
    if flt.page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"ar:l:{flt.page - 1}"))
    nav.append(
        InlineKeyboardButton(
            text=f"{flt.page + 1}/{last_page + 1}",
            callback_data=f"ar:l:{flt.page}",
        )
    )
    if flt.page < last_page:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"ar:l:{flt.page + 1}"))
    if nav:
        buttons.append(nav)

    # фильтры + закрыть
    filter_row: list[InlineKeyboardButton] = [
        InlineKeyboardButton(text="🔧 Фильтры", callback_data="ar:fmenu"),
        InlineKeyboardButton(text="✖️ Закрыть", callback_data="ar:close"),
    ]
    buttons.append(filter_row)
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _kb_filters_menu(
    flt: ArchiveFilters,
    *,
    can_filter_dept: bool,
) -> InlineKeyboardMarkup:
    def _check(active: bool) -> str:
        return "✅" if active else "▫️"

    rows: list[list[InlineKeyboardButton]] = []

    # статус
    rows.append(
        [
            InlineKeyboardButton(
                text=f"{_check(flt.status is None)} Все",
                callback_data="ar:fstat:all",
            ),
            InlineKeyboardButton(
                text=f"{_check(flt.status == 'done')} ✅ Выполнено",
                callback_data="ar:fstat:done",
            ),
            InlineKeyboardButton(
                text=f"{_check(flt.status == 'cancelled')} ❌ Отменено",
                callback_data="ar:fstat:cancelled",
            ),
        ]
    )

    # период
    rows.append(
        [
            InlineKeyboardButton(
                text=f"{_check(flt.period is None)} ⏰ За всё",
                callback_data="ar:fper:all",
            ),
            InlineKeyboardButton(
                text=f"{_check(flt.period == 'today')} Сегодня",
                callback_data="ar:fper:today",
            ),
            InlineKeyboardButton(
                text=f"{_check(flt.period == 'week')} Неделя",
                callback_data="ar:fper:week",
            ),
            InlineKeyboardButton(
                text=f"{_check(flt.period == 'month')} Месяц",
                callback_data="ar:fper:month",
            ),
        ]
    )

    # подменю «отдел» (только для lead/admin)
    if can_filter_dept:
        dept_label = "Любой отдел"
        if flt.department_id is not None:
            dept_label = "Выбран отдел…"
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"🏢 Отдел: {dept_label}",
                    callback_data="ar:fdeptmenu",
                ),
            ]
        )

    # подменю «исполнитель»
    ass_label = "Любой исполнитель"
    if flt.assignee_id is not None:
        ass_label = "Выбран исполнитель…"
    rows.append(
        [
            InlineKeyboardButton(text=f"👤 {ass_label}", callback_data="ar:fassmenu"),
        ]
    )

    rows.append(
        [
            InlineKeyboardButton(text="⬅️ К списку", callback_data="ar:l:0"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _kb_detail(task_id: int | None = None) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if task_id is not None:
        rows.append(
            [
                InlineKeyboardButton(
                    text="📎 Показать материалы",
                    callback_data=f"ar:files:{task_id}",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="⬅️ К списку", callback_data="ar:l:0")])
    rows.append([InlineKeyboardButton(text="✖️ Закрыть", callback_data="ar:close")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ---------- рендер заголовка списка ----------


def _format_header(flt: ArchiveFilters, total: int) -> str:
    parts = ["<b>📁 Архив задач</b>"]
    chips: list[str] = []
    if flt.status is None:
        chips.append("статус: все")
    else:
        chips.append("статус: " + ("выполнено" if flt.status == "done" else "отменено"))
    if flt.period is None:
        chips.append("период: за всё")
    else:
        chips.append("период: " + {"today": "сегодня", "week": "7д", "month": "30д"}[flt.period])
    parts.append("· ".join(chips))
    parts.append(f"найдено: <b>{total}</b>")
    return "\n".join(parts)


# ---------- начальный вход ----------


@router.message(F.chat.type == "private", F.text == BTN_ARCHIVE)
async def msg_archive_open(message: Message, state: FSMContext, user) -> None:
    """
    Юзер открывает архив. Сбрасываем любое предыдущее состояние и ставим
    дефолтные фильтры. Для employee жёстко фиксируем отдел.
    """
    await state.clear()
    await state.set_state(ArchiveBrowse.viewing)
    default_dept = (
        user.department_id if user.role == UserRole.EMPLOYEE.value and user.department_id else None
    )
    await state.update_data(
        **{
            DATA_DEPT: default_dept,
            DATA_ASSIGNEE: None,
            DATA_STATUS: None,
            DATA_PERIOD: None,
            DATA_PAGE: 0,
        }
    )
    await _send_list(message, state, user, edit=False)


# ---------- рендер списка ----------


async def _send_list(
    target: Message | CallbackQuery,
    state: FSMContext,
    user,
    *,
    edit: bool,
) -> None:
    data = await state.get_data()
    flt = _filters_from_data(data)
    date_from, date_to = _date_bounds(flt.period)
    can_filter_dept = user.role != UserRole.EMPLOYEE.value

    async with async_session_factory() as session:
        async with session.begin():
            total = await TasksRepository.count_archive(
                session,
                department_id=flt.department_id,
                assignee_id=flt.assignee_id,
                status=flt.status,
                date_from=date_from,
                date_to=date_to,
            )
            page = max(0, min(flt.page, max(0, (total - 1) // PAGE_SIZE)))
            if page != flt.page:
                await state.update_data(**{DATA_PAGE: page})
                flt = _filters_from_data({**data, DATA_PAGE: page})
            tasks = await TasksRepository.list_archive(
                session,
                department_id=flt.department_id,
                assignee_id=flt.assignee_id,
                status=flt.status,
                date_from=date_from,
                date_to=date_to,
                limit=PAGE_SIZE,
                offset=page * PAGE_SIZE,
            )

    rows = [(t.id, render_archive_row(t)) for t in tasks]
    if not rows:
        body = _format_header(flt, total) + "\n\nПусто. Поменяй фильтры или вернись позже."
    else:
        body = _format_header(flt, total)
    kb = _kb_list(flt, rows, total, can_filter_dept=can_filter_dept)

    if edit and isinstance(target, CallbackQuery) and target.message is not None:
        try:
            await target.message.edit_text(body, reply_markup=kb)
        except Exception as exc:  # noqa: BLE001
            logger.warning("archive: edit list failed: {}", exc)
            await target.message.answer(body, reply_markup=kb)
    elif isinstance(target, CallbackQuery) and target.message is not None:
        await target.message.answer(body, reply_markup=kb)
    elif isinstance(target, Message):
        await target.answer(body, reply_markup=kb)


# ---------- callback-handlers ----------


@router.callback_query(StateFilter(ArchiveBrowse.viewing), F.data.startswith("ar:l:"))
async def cb_list(cq: CallbackQuery, state: FSMContext, user) -> None:
    page = int(cq.data.split(":")[2])
    await state.update_data(**{DATA_PAGE: page})
    await _send_list(cq, state, user, edit=True)
    await cq.answer()


@router.callback_query(StateFilter(ArchiveBrowse.viewing), F.data == "ar:fmenu")
async def cb_fmenu(cq: CallbackQuery, state: FSMContext, user) -> None:
    data = await state.get_data()
    flt = _filters_from_data(data)
    can = user.role != UserRole.EMPLOYEE.value
    if cq.message is not None:
        await cq.message.edit_text(
            "Фильтры архива:", reply_markup=_kb_filters_menu(flt, can_filter_dept=can)
        )
    await cq.answer()


@router.callback_query(StateFilter(ArchiveBrowse.viewing), F.data.startswith("ar:fstat:"))
async def cb_fstat(cq: CallbackQuery, state: FSMContext, user) -> None:
    raw = cq.data.split(":")[2]
    value: str | None = None if raw == "all" else raw
    if value not in (None, "done", "cancelled"):
        await cq.answer("Неизвестный статус", show_alert=True)
        return
    await state.update_data(**{DATA_STATUS: value, DATA_PAGE: 0})
    await _send_list(cq, state, user, edit=True)
    await cq.answer()


@router.callback_query(StateFilter(ArchiveBrowse.viewing), F.data.startswith("ar:fper:"))
async def cb_fper(cq: CallbackQuery, state: FSMContext, user) -> None:
    raw = cq.data.split(":")[2]
    value: str | None = None if raw == "all" else raw
    if value not in (None, "today", "week", "month"):
        await cq.answer("Неизвестный период", show_alert=True)
        return
    await state.update_data(**{DATA_PERIOD: value, DATA_PAGE: 0})
    await _send_list(cq, state, user, edit=True)
    await cq.answer()


# --- подменю отделов ---


@router.callback_query(StateFilter(ArchiveBrowse.viewing), F.data == "ar:fdeptmenu")
async def cb_fdept_menu(cq: CallbackQuery, state: FSMContext, user) -> None:
    if user.role == UserRole.EMPLOYEE.value:
        await cq.answer("Доступ ограничён", show_alert=True)
        return
    async with async_session_factory() as session:
        async with session.begin():
            depts = await DepartmentsRepository.list_active(session)
    data = await state.get_data()
    current = data.get(DATA_DEPT)
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text=("✅ " if current is None else "▫️ ") + "Любой отдел",
                callback_data="ar:fdept:0",
            )
        ]
    ]
    for d in depts:
        rows.append(
            [
                InlineKeyboardButton(
                    text=("✅ " if current == d.id else "▫️ ") + d.name,
                    callback_data=f"ar:fdept:{d.id}",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="⬅️ К фильтрам", callback_data="ar:fmenu")])
    if cq.message is not None:
        await cq.message.edit_text(
            "Выбери отдел:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
        )
    await cq.answer()


@router.callback_query(StateFilter(ArchiveBrowse.viewing), F.data.startswith("ar:fdept:"))
async def cb_fdept(cq: CallbackQuery, state: FSMContext, user) -> None:
    if user.role == UserRole.EMPLOYEE.value:
        await cq.answer("Доступ ограничён", show_alert=True)
        return
    raw = int(cq.data.split(":")[2])
    value: int | None = None if raw == 0 else raw
    # смена отдела сбрасывает assignee — он мог быть из другого отдела
    await state.update_data(**{DATA_DEPT: value, DATA_ASSIGNEE: None, DATA_PAGE: 0})
    await _send_list(cq, state, user, edit=True)
    await cq.answer()


# --- подменю исполнителей ---


@router.callback_query(StateFilter(ArchiveBrowse.viewing), F.data == "ar:fassmenu")
async def cb_fass_menu(cq: CallbackQuery, state: FSMContext, user) -> None:
    data = await state.get_data()
    flt = _filters_from_data(data)
    async with async_session_factory() as session:
        async with session.begin():
            assignee_ids = await TasksRepository.list_assignees_with_archive(
                session, department_id=flt.department_id
            )
            users = await UsersRepository.list_by_ids(session, assignee_ids)
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text=("✅ " if flt.assignee_id is None else "▫️ ") + "Любой исполнитель",
                callback_data="ar:fass:0",
            )
        ]
    ]
    if not users:
        rows.append(
            [
                InlineKeyboardButton(
                    text="(нет архивных исполнителей по выбранному отделу)",
                    callback_data="ar:fmenu",
                )
            ]
        )
    for u in users:
        label = u.full_name or (f"@{u.tg_username}" if u.tg_username else f"#{u.id}")
        rows.append(
            [
                InlineKeyboardButton(
                    text=("✅ " if flt.assignee_id == u.id else "▫️ ") + label,
                    callback_data=f"ar:fass:{u.id}",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="⬅️ К фильтрам", callback_data="ar:fmenu")])
    if cq.message is not None:
        await cq.message.edit_text(
            "Выбери исполнителя:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )
    await cq.answer()


@router.callback_query(StateFilter(ArchiveBrowse.viewing), F.data.startswith("ar:fass:"))
async def cb_fass(cq: CallbackQuery, state: FSMContext, user) -> None:
    raw = int(cq.data.split(":")[2])
    value: int | None = None if raw == 0 else raw
    await state.update_data(**{DATA_ASSIGNEE: value, DATA_PAGE: 0})
    await _send_list(cq, state, user, edit=True)
    await cq.answer()


# --- детальная карточка задачи ---


@router.callback_query(StateFilter(ArchiveBrowse.viewing), F.data.startswith("ar:t:"))
async def cb_task(cq: CallbackQuery, state: FSMContext, user) -> None:
    task_id = int(cq.data.split(":")[2])
    async with async_session_factory() as session:
        async with session.begin():
            task = await TasksRepository.get_full(session, task_id)
            history = await HistoryRepository.list_by_task(session, task_id) if task else []
            actor_ids = {h.user_id for h in history if h.user_id is not None}
            users = await UsersRepository.list_by_ids(session, actor_ids) if actor_ids else []
            link_chat_id = await AppSettingsRepository.get_int(session, KEY_TASK_CHAT_ID)
            link_topic_id = await AppSettingsRepository.get_int(session, KEY_LEADERSHIP_TOPIC_ID)
    if task is None:
        await cq.answer("Задача не найдена", show_alert=True)
        return

    users_by_id = {u.id: u for u in users}
    creator = task.creator
    assignee = task.assignee
    files_count = len(task.files)

    card = render_task_card(
        task,
        creator=creator,
        assignee=assignee,
        department=task.department,
        files_count=files_count,
        task_chat_id=link_chat_id,
        lead_topic_id=link_topic_id,
    )
    # для архива дополним блок с completed_at / cancelled_at
    extra: list[str] = []
    if task.completed_at:
        extra.append(f"🕘 Закрыта: {format_dt_local(task.completed_at)}")
    hist_text = render_task_history(history, users_by_id=users_by_id)
    body = card
    if extra:
        body += "\n" + "\n".join(extra)
    body += "\n\n<b>История:</b>\n" + hist_text

    if cq.message is not None:
        try:
            await cq.message.edit_text(body, reply_markup=_kb_detail(task.id))
        except Exception as exc:  # noqa: BLE001
            logger.warning("archive: edit detail failed: {}", exc)
            await cq.message.answer(body, reply_markup=_kb_detail(task.id))
    await cq.answer()


# --- материалы по этапам ---


@router.callback_query(StateFilter(ArchiveBrowse.viewing), F.data.startswith("ar:files:"))
async def cb_view_files(cq: CallbackQuery) -> None:
    """Досылает в DM все материалы задачи: создание / ответы / правки / вопросы.

    Группировка по purpose / событиям:
      - creation: task_files с purpose='creation' (вложения постановщика).
      - completion: task_files с purpose='completion' (отчёты исполнителя).
        Могут включать несколько циклов rework→submit — все совокупно.
      - rework: file_id из payload событий rework_requested (правки от
        постановщика; в task_files не хранятся, только в payload).
      - question / answer: file из payload событий question_asked /
        question_answered.

    Каждая группа предваряется header-сообщением. Если файлов нет — alert.
    """
    try:
        task_id = int((cq.data or "").split(":")[-1])
    except (ValueError, IndexError):
        await cq.answer("Некорректная задача.", show_alert=True)
        return

    async with async_session_factory() as session:
        task = await TasksRepository.get_full(session, task_id)
        if task is None:
            await cq.answer("Задача не найдена.", show_alert=True)
            return
        history = await HistoryRepository.list_by_task(session, task_id)

    creation_files = [f for f in task.files if f.purpose == "creation"]
    completion_files = [f for f in task.files if f.purpose == "completion"]
    rework_files: list[dict] = []
    question_files: list[dict] = []
    answer_files: list[dict] = []
    for ev in history:
        payload = ev.payload or {}
        if ev.event_type == "rework_requested":
            for f in payload.get("rework_files") or []:
                if f.get("file_id"):
                    rework_files.append(f)
        elif ev.event_type == "question_asked":
            f = payload.get("file")
            if f and f.get("file_id"):
                question_files.append(f)
        elif ev.event_type == "question_answered":
            f = payload.get("file")
            if f and f.get("file_id"):
                answer_files.append(f)

    total = (
        len(creation_files)
        + len(completion_files)
        + len(rework_files)
        + len(question_files)
        + len(answer_files)
    )
    if total == 0:
        await cq.answer("По этой задаче нет вложений.", show_alert=True)
        return

    if cq.message is None:
        await cq.answer()
        return
    chat_id = cq.message.chat.id

    bot = cq.bot

    async def _send_group(header: str, files: list, file_attr: bool) -> None:
        if not files:
            return
        await bot.send_message(chat_id, header)
        for f in files:
            kind = (f.kind if file_attr else f.get("kind")) or "document"
            file_id = f.tg_file_id if file_attr else f.get("file_id")
            file_name = f.file_name if file_attr else f.get("file_name")
            if not file_id:
                continue
            try:
                if kind == "photo":
                    await bot.send_photo(chat_id, photo=file_id)
                elif kind == "video":
                    await bot.send_video(chat_id, video=file_id)
                elif kind == "animation":
                    await bot.send_animation(chat_id, animation=file_id)
                else:
                    await bot.send_document(chat_id, document=file_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "archive ar:files task #{} send failed ({}): {}",
                    task_id,
                    file_name or file_id,
                    exc,
                )

    await _send_group(
        f"📎 <b>Материалы по задаче #{task.display_number}</b> · создание ({len(creation_files)})",
        creation_files,
        file_attr=True,
    )
    await _send_group(
        f"📨 <b>Ответы исполнителя</b> ({len(completion_files)})",
        completion_files,
        file_attr=True,
    )
    await _send_group(
        f"✏️ <b>Правки постановщика</b> ({len(rework_files)})",
        rework_files,
        file_attr=False,
    )
    await _send_group(
        f"❓ <b>Файлы из вопросов</b> ({len(question_files)})",
        question_files,
        file_attr=False,
    )
    await _send_group(
        f"💬 <b>Файлы из ответов</b> ({len(answer_files)})",
        answer_files,
        file_attr=False,
    )

    await cq.answer(f"Отправил {total} вложений в чат ниже.")


# --- выход ---


@router.callback_query(StateFilter(ArchiveBrowse.viewing), F.data == "ar:close")
async def cb_close(cq: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    if cq.message is not None:
        try:
            await cq.message.edit_text("Архив закрыт.")
        except Exception:  # noqa: BLE001
            pass
    await cq.answer()
