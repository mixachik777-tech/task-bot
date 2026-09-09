"""
Меню «📊 Статистика» — доступно только lead/admin.

Сводки строит AnalyticsRepository. UI: inline-переключатели scope и period,
сообщение редактируется на месте.

scope:
  user       — задачи, где юзер creator или assignee
  department — задачи своего отдела (для lead) или выбранного (для admin)
  all        — все (только admin)

period: today | week (rolling 7 дней) | month (rolling 30 дней).
"""

from __future__ import annotations

import html

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Message,
)
from loguru import logger

from app.bot.keyboards.main_menu import BTN_STATS
from app.bot.utils.charts import render_productivity_chart
from app.db.base import async_session_factory
from app.db.enums import UserRole
from app.db.models import User
from app.db.repositories.analytics import (
    AnalyticsRepository,
    AnalyticsSummary,
    UserProductivity,
)
from app.db.repositories.users import UsersRepository

router = Router(name="analytics")


class AnalyticsBrowse(StatesGroup):
    viewing = State()


DATA_SCOPE = "an_scope"
DATA_PERIOD = "an_period"

SCOPE_LABEL = {
    "user": "👤 Я",
    "department": "🏢 Мой отдел",
    "all": "🌐 Все",
}
PERIOD_LABEL = {
    "today": "Сегодня",
    "week": "Неделя",
    "month": "Месяц",
}


def _allowed_scopes(viewer: User) -> list[str]:
    """
    Список scope, доступных конкретному юзеру.

    Важно: `department` НЕ возвращается, если у viewer нет `department_id`,
    иначе при клике падает `_scope_predicate` с
    «scope=department requires department_id».
    """
    role = viewer.role
    has_dept = viewer.department_id is not None
    if role == UserRole.ADMIN.value:
        return ["user", "department", "all"] if has_dept else ["user", "all"]
    if role == UserRole.LEAD.value:
        return ["user", "department"] if has_dept else ["user"]
    return []  # employee сюда вообще не должен попадать


def _safe_scope(viewer: User, scope: str | None) -> str:
    """
    Нормализует scope под текущие возможности viewer.

    Используется ПОСЛЕ чтения из FSM-state: если в state застрял невалидный
    scope (например, юзер раньше уже выбрал «Мой отдел», а потом ему
    department_id отозвали) — мягко падаем на дефолт без исключения.
    """
    allowed = _allowed_scopes(viewer)
    if scope in allowed:
        return scope
    # дефолт по приоритету: все / отдел / только мои
    for fallback in ("all", "department", "user"):
        if fallback in allowed:
            return fallback
    return "user"


def _format_avg(seconds: float | None) -> str:
    """
    Возвращает человекочитаемую длительность.

    Не содержит угловых скобок: значение оборачивается в `<b>…</b>` при
    рендере, поэтому «<1мин» сломает HTML-парс Telegram. Используем
    «менее минуты».
    """
    if seconds is None:
        return "—"
    total = int(seconds)
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}д")
    if hours:
        parts.append(f"{hours}ч")
    if minutes and not days:
        parts.append(f"{minutes}мин")
    return " ".join(parts) or "менее минуты"


def _format_summary(
    summary: AnalyticsSummary, scope: str, period: str
) -> str:
    scope_human = {
        "user": "Только мои",
        "department": "Мой отдел",
        "all": "Все задачи",
    }[scope]
    period_human = {
        "today": "сегодня",
        "week": "за 7 дней",
        "month": "за 30 дней",
    }[period]
    return "\n".join(
        [
            f"<b>📊 Статистика — {scope_human} · {period_human}</b>",
            "",
            f"🆕 Создано: <b>{summary.created}</b>",
            f"✅ Выполнено: <b>{summary.completed}</b>",
            f"🟡 В работе сейчас: <b>{summary.in_progress}</b>",
            f"🔴 Просрочено сейчас: <b>{summary.overdue}</b>",
            f"⏱ Среднее время выполнения: <b>{_format_avg(summary.avg_completion_seconds)}</b>",
        ]
    )


def _kb(scope: str, period: str, viewer: User) -> InlineKeyboardMarkup:
    def _mark(active: bool) -> str:
        return "✅" if active else "▫️"

    scopes = _allowed_scopes(viewer)
    rows: list[list[InlineKeyboardButton]] = []
    rows.append(
        [
            InlineKeyboardButton(
                text=f"{_mark(scope == s)} {SCOPE_LABEL[s]}",
                callback_data=f"an:s:{s}",
            )
            for s in scopes
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                text=f"{_mark(period == p)} {PERIOD_LABEL[p]}",
                callback_data=f"an:p:{p}",
            )
            for p in ("today", "week", "month")
        ]
    )
    rows.append(
        [InlineKeyboardButton(text="👤 Продуктивность", callback_data="an:prod")]
    )
    rows.append([InlineKeyboardButton(text="✖️ Закрыть", callback_data="an:close")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _compute(user: User, scope: str, period: str) -> AnalyticsSummary:
    async with async_session_factory() as session:
        async with session.begin():
            return await AnalyticsRepository.get_summary(
                session,
                scope=scope,
                period=period,
                user_id=user.id if scope == "user" else None,
                department_id=user.department_id if scope == "department" else None,
            )


@router.message(F.chat.type == "private", F.text == BTN_STATS)
async def msg_stats_open(message: Message, state: FSMContext, user: User) -> None:
    role = user.role
    if role not in (UserRole.LEAD.value, UserRole.ADMIN.value):
        await message.answer("📊 Статистика доступна только руководителям и админам.")
        return
    if not user.is_active:
        await message.answer("⏳ Аккаунт ожидает одобрения администратором.")
        return
    # дефолт по приоритету: admin без отдела → all; lead без отдела → user
    if role == UserRole.ADMIN.value:
        scope = "all"
    elif user.department_id:
        scope = "department"
    else:
        scope = "user"
    period = "week"

    await state.clear()
    await state.set_state(AnalyticsBrowse.viewing)

    summary = await _compute(user, scope, period)
    await state.update_data(**{DATA_SCOPE: scope, DATA_PERIOD: period})
    await message.answer(
        _format_summary(summary, scope, period),
        reply_markup=_kb(scope, period, user),
    )


@router.callback_query(StateFilter(AnalyticsBrowse.viewing), F.data.startswith("an:s:"))
async def cb_scope(cq: CallbackQuery, state: FSMContext, user: User) -> None:
    scope = cq.data.split(":")[2]
    if scope not in _allowed_scopes(user):
        await cq.answer("Этот режим тебе недоступен", show_alert=True)
        return
    data = await state.get_data()
    period = data.get(DATA_PERIOD, "week")
    summary = await _compute(user, scope, period)
    # state обновляем ПОСЛЕ успешного _compute — чтобы невалидное значение
    # не застряло в FSM и не ломало последующие переходы
    await state.update_data(**{DATA_SCOPE: scope})
    if cq.message is not None:
        try:
            await cq.message.edit_text(
                _format_summary(summary, scope, period),
                reply_markup=_kb(scope, period, user),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("analytics: edit failed: {}", exc)
    await cq.answer()


@router.callback_query(StateFilter(AnalyticsBrowse.viewing), F.data.startswith("an:p:"))
async def cb_period(cq: CallbackQuery, state: FSMContext, user: User) -> None:
    period = cq.data.split(":")[2]
    if period not in ("today", "week", "month"):
        await cq.answer("Неизвестный период", show_alert=True)
        return
    data = await state.get_data()
    scope = _safe_scope(user, data.get(DATA_SCOPE))
    summary = await _compute(user, scope, period)
    await state.update_data(**{DATA_PERIOD: period, DATA_SCOPE: scope})
    if cq.message is not None:
        try:
            await cq.message.edit_text(
                _format_summary(summary, scope, period),
                reply_markup=_kb(scope, period, user),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("analytics: edit failed: {}", exc)
    await cq.answer()


# --- Раздел «👤 Продуктивность сотрудника» ---

DATA_PROD_USER = "an_prod_user"
DATA_PROD_PERIOD = "an_prod_period"

PROD_PERIOD_LABEL = {
    "today": "сегодня",
    "week": "за 7 дней",
    "month": "за 30 дней",
}


def _kb_prod_user_list(users: list) -> InlineKeyboardMarkup:
    """
    Один сводный список всех активных сотрудников. Без выбора отдела:
    в task-bot топики открыты всем, отдел это тег задачи, а не группа
    юзеров — поэтому фильтровать список людей по отделу смысла нет.
    Название отдела добавлено в подпись кнопки как контекст.
    """
    rows: list[list[InlineKeyboardButton]] = []
    for u in users:
        name = u.full_name or (f"@{u.tg_username}" if u.tg_username else f"#{u.id}")
        dept = u.department.name if getattr(u, "department", None) else "—"
        label = f"{name} · {dept}"
        # Telegram-кнопки имеют лимит длины ~64 символа на text. Не страшно
        # для имён до 30 + название отдела до 20.
        if len(label) > 50:
            label = label[:47] + "…"
        rows.append(
            [InlineKeyboardButton(text=label, callback_data=f"an:pu:{u.id}")]
        )
    rows.append(
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="an:pback")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _kb_prod_card(user_id: int, period: str) -> InlineKeyboardMarkup:
    def _mark(active: bool) -> str:
        return "✅" if active else "▫️"

    rows: list[list[InlineKeyboardButton]] = []
    rows.append(
        [
            InlineKeyboardButton(
                text=f"{_mark(period == p)} {PERIOD_LABEL[p]}",
                callback_data=f"an:pp:{p}:{user_id}",
            )
            for p in ("today", "week", "month")
        ]
    )
    rows.append(
        [InlineKeyboardButton(text="⬅️ К общей", callback_data="an:pback")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _role_ru(role: str) -> str:
    return {
        UserRole.EMPLOYEE.value: "сотрудник",
        UserRole.LEAD.value: "руководитель",
        UserRole.ADMIN.value: "админ",
    }.get(role, role)


def _format_prod_caption(
    target: User, prod: UserProductivity, period: str
) -> str:
    name = html.escape(target.full_name or f"id={target.tg_user_id}")
    handle = f"@{html.escape(target.tg_username)}" if target.tg_username else ""
    dept_name = (
        html.escape(target.department.name)
        if getattr(target, "department", None)
        else "—"
    )
    period_human = PROD_PERIOD_LABEL[period]
    lines = [
        f"<b>👤 {name}</b>" + (f" · {handle}" if handle else ""),
        f"🏢 {dept_name} · {_role_ru(target.role)}",
        f"🗓 {period_human}",
        "",
        f"📥 Назначено: <b>{prod.assigned}</b>",
        f"✅ Выполнил: <b>{prod.completed}</b>",
        f"🟡 В работе сейчас: <b>{prod.in_progress_now}</b>",
        f"🔴 Просрочено сейчас: <b>{prod.overdue_now}</b>",
        f"⏱ Среднее время выполнения: <b>{_format_avg(prod.avg_completion_seconds)}</b>",
    ]
    return "\n".join(lines)


async def _load_target_user(user_id: int) -> User | None:
    async with async_session_factory() as session:
        async with session.begin():
            from sqlalchemy import select
            from sqlalchemy.orm import selectinload

            result = await session.execute(
                select(User)
                .options(selectinload(User.department))
                .where(User.id == user_id)
            )
            return result.scalar_one_or_none()


async def _compute_prod(user_id: int, period: str) -> UserProductivity:
    async with async_session_factory() as session:
        async with session.begin():
            return await AnalyticsRepository.get_user_productivity(
                session, user_id=user_id, period=period
            )


def _can_view_target(viewer: User, target: User) -> bool:
    """
    lead/admin видят всех активных сотрудников.

    Прежняя логика «lead — только свой отдел» снята: топики открыты всем,
    сотрудники работают по задачам разных направлений, отдел юзера это
    формальный тег, а не группа доступа. Запрет смотреть «не свой отдел»
    был искусственным.
    """
    if viewer.role not in (UserRole.LEAD.value, UserRole.ADMIN.value):
        return False
    return target.is_active


@router.callback_query(StateFilter(AnalyticsBrowse.viewing), F.data == "an:prod")
async def cb_prod_open(cq: CallbackQuery, state: FSMContext, user: User) -> None:
    if user.role not in (UserRole.LEAD.value, UserRole.ADMIN.value):
        await cq.answer("Только для руководителей и админов", show_alert=True)
        return
    async with async_session_factory() as session:
        async with session.begin():
            users = list(
                await UsersRepository.list_active_with_department(session)
            )
    if not users:
        if cq.message is not None:
            await cq.message.edit_text(
                "Активных сотрудников нет.",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="⬅️ Назад", callback_data="an:pback")]
                    ]
                ),
            )
        await cq.answer()
        return
    if cq.message is not None:
        await cq.message.edit_text(
            "Выбери сотрудника:", reply_markup=_kb_prod_user_list(users)
        )
    await cq.answer()


@router.callback_query(StateFilter(AnalyticsBrowse.viewing), F.data.startswith("an:pu:"))
async def cb_prod_user_pick(cq: CallbackQuery, state: FSMContext, user: User) -> None:
    target_id = int(cq.data.split(":")[2])
    target = await _load_target_user(target_id)
    if target is None:
        await cq.answer("Сотрудник не найден", show_alert=True)
        return
    if not _can_view_target(user, target):
        await cq.answer("Этот сотрудник вне твоего отдела", show_alert=True)
        return

    period = "week"
    await state.update_data(**{DATA_PROD_USER: target.id, DATA_PROD_PERIOD: period})

    prod = await _compute_prod(target.id, period)
    img = render_productivity_chart(
        prod.daily_breakdown,
        user_name=target.full_name or f"id={target.tg_user_id}",
        period_label=PROD_PERIOD_LABEL[period].capitalize(),
    )
    caption = _format_prod_caption(target, prod, period)
    kb = _kb_prod_card(target.id, period)

    if cq.message is not None:
        try:
            await cq.message.delete()
        except Exception as exc:  # noqa: BLE001
            logger.debug("prod: delete old msg failed: {}", exc)
        await cq.message.answer_photo(
            BufferedInputFile(img, filename="productivity.png"),
            caption=caption,
            reply_markup=kb,
        )
    await cq.answer()


@router.callback_query(StateFilter(AnalyticsBrowse.viewing), F.data.startswith("an:pp:"))
async def cb_prod_period(cq: CallbackQuery, state: FSMContext, user: User) -> None:
    parts = cq.data.split(":")
    period = parts[2]
    target_id = int(parts[3])
    if period not in ("today", "week", "month"):
        await cq.answer("Неизвестный период", show_alert=True)
        return
    target = await _load_target_user(target_id)
    if target is None or not _can_view_target(user, target):
        await cq.answer("Сотрудник не найден / нет доступа", show_alert=True)
        return

    await state.update_data(**{DATA_PROD_USER: target.id, DATA_PROD_PERIOD: period})

    prod = await _compute_prod(target.id, period)
    img = render_productivity_chart(
        prod.daily_breakdown,
        user_name=target.full_name or f"id={target.tg_user_id}",
        period_label=PROD_PERIOD_LABEL[period].capitalize(),
    )
    caption = _format_prod_caption(target, prod, period)
    kb = _kb_prod_card(target.id, period)

    media = InputMediaPhoto(
        media=BufferedInputFile(img, filename="productivity.png"),
        caption=caption,
        parse_mode="HTML",
    )
    if cq.message is not None:
        try:
            await cq.message.edit_media(media, reply_markup=kb)
        except Exception as exc:  # noqa: BLE001
            logger.warning("prod: edit_media failed, fallback delete+send: {}", exc)
            try:
                await cq.message.delete()
            except Exception:  # noqa: BLE001
                pass
            await cq.message.answer_photo(
                BufferedInputFile(img, filename="productivity.png"),
                caption=caption,
                reply_markup=kb,
            )
    await cq.answer()


@router.callback_query(StateFilter(AnalyticsBrowse.viewing), F.data == "an:pback")
async def cb_prod_back(cq: CallbackQuery, state: FSMContext, user: User) -> None:
    """Возврат к общей карточке статистики. Если текущее сообщение — фото,
    удаляем и шлём новое текстовое; иначе просто edit_text."""
    data = await state.get_data()
    scope = _safe_scope(user, data.get(DATA_SCOPE))
    period = data.get(DATA_PERIOD, "week")
    summary = await _compute(user, scope, period)
    await state.update_data(**{DATA_SCOPE: scope, DATA_PERIOD: period})
    body = _format_summary(summary, scope, period)
    kb = _kb(scope, period, user)

    if cq.message is not None:
        if cq.message.photo:
            try:
                await cq.message.delete()
            except Exception:  # noqa: BLE001
                pass
            await cq.message.answer(body, reply_markup=kb)
        else:
            try:
                await cq.message.edit_text(body, reply_markup=kb)
            except Exception as exc:  # noqa: BLE001
                logger.warning("prod-back: edit failed: {}", exc)
                await cq.message.answer(body, reply_markup=kb)
    await cq.answer()


@router.callback_query(StateFilter(AnalyticsBrowse.viewing), F.data == "an:close")
async def cb_close(cq: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    if cq.message is not None:
        try:
            if cq.message.photo:
                await cq.message.delete()
                await cq.message.answer("Статистика закрыта.")
            else:
                await cq.message.edit_text("Статистика закрыта.")
        except Exception:  # noqa: BLE001
            pass
    await cq.answer()
