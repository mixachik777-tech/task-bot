"""
Личный кабинет «👤 Кабинет» — одна страница на пользователя.

Доступен всем активным: employee / lead / admin. Показывает:
1) числовые метрики продуктивности за период (today/week/month),
2) активные задачи юзера, разложенные по температуре дедлайна,
3) залипшие (in_progress > 7 дней без событий),
4) кнопку «Свободные задачи» — список без assignee по всем отделам.

Состояние выбранного периода держится во FSM (CabinetBrowse.viewing).
Карточка перерисовывается через edit_text — без множества сообщений.
"""

from __future__ import annotations

import html
from datetime import datetime, timezone

from aiogram import Bot, F, Router
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

from app.bot.keyboards.main_menu import BTN_CABINET
from app.bot.keyboards.task_card import build_approval_kb, build_task_card_kb
from app.bot.utils.render import render_task_card
from app.bot.utils.time import format_delta
from app.db.base import async_session_factory
from app.db.models import Task, User
from app.db.repositories.analytics import (
    AnalyticsRepository,
    ActiveTasksBuckets,
    UserProductivity,
)
from app.db.repositories.app_settings import (
    KEY_LEADERSHIP_TOPIC_ID,
    KEY_TASK_CHAT_ID,
    AppSettingsRepository,
)
from app.db.repositories.tasks import TasksRepository

router = Router(name="cabinet")

# ── FSM ──────────────────────────────────────────────────────────────


class CabinetBrowse(StatesGroup):
    viewing = State()


DATA_PERIOD = "cab_period"

PERIOD_LABEL = {
    "today": "Сегодня",
    "week": "Неделя",
    "month": "Месяц",
}
PERIOD_HUMAN = {
    "today": "сегодня",
    "week": "за 7 дней",
    "month": "за 30 дней",
}
MAX_PREVIEW_PER_BUCKET = 5
MAX_FREE_TASKS = 20
MAX_STALE_TASKS = 10
STALE_DAYS = 7

# ── format helpers ───────────────────────────────────────────────────


def _format_avg(seconds: float | None) -> str:
    """Длительность в человекочитаемом виде; без угловых скобок (HTML)."""
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


def _format_pct(value: float | None) -> str:
    if value is None:
        return "—"
    # 100.0 → «100%», 66.7 → «67%». В кабинете округляем до целого, чтобы
    # глазу не пестрило — точность всё равно зависит от объёма выборки.
    return f"{round(value)}%"


def _task_line(task: Task, now_utc: datetime) -> str:
    """Одна строка задачи в текстовом списке кабинета.

    Этап Г: после переезда задач в личку deeplink на топик отдела больше
    не имеет смысла (broadcast-задачи живут в DM, не в супергруппе).
    Открыть карточку конкретной задачи можно через отдельную кнопку
    «🔎 Открыть карточку задачи» в кабинете → inline-список.
    """
    title = (task.title or "").strip() or "(без названия)"
    if len(title) > 40:
        title = title[:39] + "…"
    delta_s = int((task.deadline - now_utc).total_seconds())
    return f"• #{task.display_number} {html.escape(title)} — {format_delta(delta_s)}"


def _bucket_block(title: str, tasks: list[Task], now_utc: datetime) -> str:
    if not tasks:
        return ""
    head = f"<b>{title}</b> ({len(tasks)})"
    preview = tasks[:MAX_PREVIEW_PER_BUCKET]
    lines = [_task_line(t, now_utc) for t in preview]
    if len(tasks) > MAX_PREVIEW_PER_BUCKET:
        lines.append(f"…и ещё {len(tasks) - MAX_PREVIEW_PER_BUCKET}")
    return head + "\n" + "\n".join(lines)


def _format_cabinet(
    user: User,
    prod: UserProductivity,
    buckets: ActiveTasksBuckets,
    stale: list[Task],
    period: str,
) -> str:
    name = html.escape(user.full_name or f"id={user.tg_user_id}")
    handle = f" · @{html.escape(user.tg_username)}" if user.tg_username else ""
    dept = html.escape(user.department.name) if getattr(user, "department", None) else "—"
    period_human = PERIOD_HUMAN[period]

    header = [
        f"<b>👤 {name}</b>{handle}",
        f"🏢 {dept}",
        f"🗓 Период: {period_human}",
        "",
        "<b>📊 Цифры</b>",
        f"📥 Назначено: <b>{prod.assigned}</b>",
        f"✅ Выполнено: <b>{prod.completed}</b> "
        f"(% выполнения: <b>{_format_pct(prod.completion_rate_pct)}</b>)",
        f"🎯 В срок: <b>{prod.on_time_count}</b> "
        f"(% в срок: <b>{_format_pct(prod.on_time_rate_pct)}</b>)",
        f"✖️ Отменено: <b>{prod.cancelled}</b>",
        f"⏱ Среднее время выполнения: <b>{_format_avg(prod.avg_completion_seconds)}</b>",
        "",
    ]

    # активные — раскладка по корзинам
    active_lines: list[str] = [
        f"<b>📂 Активные сейчас (всего {buckets.total})</b>",
    ]
    if buckets.total == 0:
        active_lines.append("— нет задач в работе")
    else:
        blocks = [
            _bucket_block("🔴 Просрочено", buckets.overdue, buckets.now_utc),
            _bucket_block("🔥 Горит ≤24ч", buckets.burning_24h, buckets.now_utc),
            _bucket_block("🟡 Сегодня", buckets.today, buckets.now_utc),
            _bucket_block("📅 На неделе", buckets.this_week, buckets.now_utc),
            _bucket_block("📆 Позже", buckets.later, buckets.now_utc),
        ]
        for b in blocks:
            if b:
                active_lines.append(b)

    body = "\n".join(header) + "\n".join(active_lines)

    # залипшие — только если есть
    if stale:
        body += "\n\n" + (
            f"<b>💤 Залипли (без движения &gt; {STALE_DAYS} дн.)</b>\n"
            + "\n".join(_task_line(t, buckets.now_utc) for t in stale[:MAX_STALE_TASKS])
        )
        if len(stale) > MAX_STALE_TASKS:
            body += f"\n…и ещё {len(stale) - MAX_STALE_TASKS}"

    return body


def _kb(period: str, *, is_admin: bool = False) -> InlineKeyboardMarkup:
    def _mark(active: bool) -> str:
        return "✅" if active else "▫️"

    rows: list[list[InlineKeyboardButton]] = []
    rows.append(
        [
            InlineKeyboardButton(
                text=f"{_mark(period == p)} {PERIOD_LABEL[p]}",
                callback_data=f"cab:p:{p}",
            )
            for p in ("today", "week", "month")
        ]
    )
    rows.append([InlineKeyboardButton(text="🔎 Открыть карточку", callback_data="cab:taskmenu")])
    rows.append([InlineKeyboardButton(text="🎯 Свободные задачи", callback_data="cab:free")])
    if is_admin:
        rows.append([InlineKeyboardButton(text="📑 Все задачи (админ)", callback_data="at:page:0")])
        rows.append(
            [InlineKeyboardButton(text="📋 Активные у сотрудников", callback_data="wl:list")]
        )
        rows.append([InlineKeyboardButton(text="🛡 Лиды чата", callback_data="adm:leads")])
    rows.append([InlineKeyboardButton(text="🔄 Обновить", callback_data="cab:refresh")])
    rows.append([InlineKeyboardButton(text="✖️ Закрыть", callback_data="cab:close")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ── data loaders ─────────────────────────────────────────────────────


async def _load_full(
    user_id: int, period: str
) -> tuple[User, UserProductivity, ActiveTasksBuckets, list[Task]]:
    """Один запрос за раз, чтобы не плодить параллельные сессии — все
    запросы внутри одного транзакционного скоупа."""
    async with async_session_factory() as session:
        async with session.begin():
            from sqlalchemy import select
            from sqlalchemy.orm import selectinload

            result = await session.execute(
                select(User).options(selectinload(User.department)).where(User.id == user_id)
            )
            target = result.scalar_one()
            prod = await AnalyticsRepository.get_user_productivity(
                session, user_id=user_id, period=period
            )
            buckets = await AnalyticsRepository.get_user_active_tasks(session, user_id=user_id)
            stale = await AnalyticsRepository.get_user_stale_tasks(
                session, user_id=user_id, stale_days=STALE_DAYS
            )
    return target, prod, buckets, stale


async def _load_free_tasks(*, department_id: int | None) -> list[Task]:
    """Загружает свободные задачи (status=new, assignee=NULL).

    Если department_id передан — отфильтровано по нему (employee/lead не должны
    видеть и принимать чужие отделы). Для admin (без отдела) — все.
    """
    async with async_session_factory() as session:
        async with session.begin():
            tasks = await TasksRepository.list_unassigned(
                session, limit=MAX_FREE_TASKS, department_id=department_id
            )
            return list(tasks)


# ── handlers ─────────────────────────────────────────────────────────


@router.message(F.chat.type == "private", F.text == BTN_CABINET)
async def msg_cabinet_open(message: Message, state: FSMContext, user: User) -> None:
    if not user.is_active:
        await message.answer("⏳ Аккаунт ожидает одобрения администратором.")
        return
    period = "week"
    await state.clear()
    await state.set_state(CabinetBrowse.viewing)
    await state.update_data(**{DATA_PERIOD: period})

    target, prod, buckets, stale = await _load_full(user.id, period)
    is_admin = user.role == "admin"
    await message.answer(
        _format_cabinet(target, prod, buckets, stale, period),
        reply_markup=_kb(period, is_admin=is_admin),
    )


@router.callback_query(StateFilter(CabinetBrowse.viewing), F.data.startswith("cab:p:"))
async def cb_period(cq: CallbackQuery, state: FSMContext, user: User) -> None:
    period = cq.data.split(":")[2]
    if period not in PERIOD_LABEL:
        await cq.answer("Неизвестный период", show_alert=True)
        return
    await state.update_data(**{DATA_PERIOD: period})
    target, prod, buckets, stale = await _load_full(user.id, period)
    body = _format_cabinet(target, prod, buckets, stale, period)
    is_admin = user.role == "admin"
    if cq.message is not None:
        try:
            await cq.message.edit_text(body, reply_markup=_kb(period, is_admin=is_admin))
        except Exception as exc:  # noqa: BLE001
            logger.warning("cabinet: edit failed: {}", exc)
    await cq.answer()


@router.callback_query(StateFilter(CabinetBrowse.viewing), F.data == "cab:refresh")
async def cb_refresh(cq: CallbackQuery, state: FSMContext, user: User) -> None:
    data = await state.get_data()
    period = data.get(DATA_PERIOD, "week")
    target, prod, buckets, stale = await _load_full(user.id, period)
    body = _format_cabinet(target, prod, buckets, stale, period)
    is_admin = user.role == "admin"
    not_modified = False
    if cq.message is not None:
        try:
            await cq.message.edit_text(body, reply_markup=_kb(period, is_admin=is_admin))
        except Exception as exc:  # noqa: BLE001
            # Telegram кидает «message not modified», если ничего не поменялось —
            # отдадим юзеру явный «без изменений» вместо обманчивого «Обновлено».
            if "not modified" in str(exc).lower():
                not_modified = True
            else:
                logger.warning("cabinet refresh: edit failed: {}", exc)
    await cq.answer("Без изменений" if not_modified else "Обновлено")


def _free_tasks_kb(tasks: list[Task]) -> InlineKeyboardMarkup:
    """Inline-клавиатура раздела «Свободные»: по кнопке на задачу + назад.

    Тап на задачу открывает предпросмотр (cab:free_open), а не сразу
    принимает в работу — юзер должен увидеть условия (срок, описание,
    файлы постановщика) до того как взять обязательство.
    """
    rows: list[list[InlineKeyboardButton]] = []
    for t in tasks:
        title = (t.title or "").strip() or "(без названия)"
        if len(title) > 32:
            title = title[:31] + "…"
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"👁 #{t.display_number} {title}",
                    callback_data=f"cab:free_open:{t.id}",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="⬅️ В кабинет", callback_data="cab:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(StateFilter(CabinetBrowse.viewing), F.data == "cab:free")
async def cb_free_tasks(cq: CallbackQuery, state: FSMContext, user: User) -> None:
    # admin видит все отделы; employee/lead — только свой
    department_id = None if user.role == "admin" else user.department_id
    tasks = await _load_free_tasks(department_id=department_id)
    now_utc = datetime.now(timezone.utc)
    if not tasks:
        text = "🎯 <b>Свободные задачи</b>\n\nСейчас нет задач без исполнителя."
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="⬅️ В кабинет", callback_data="cab:back")]]
        )
    else:
        scope = "по всем отделам" if user.role == "admin" else "в твоём отделе"
        lines = [f"🎯 <b>Свободные задачи</b> ({scope})", ""]
        for t in tasks:
            dept = html.escape(t.department.name) if getattr(t, "department", None) else "—"
            base = _task_line(t, now_utc)
            head, _sep, tail = base.partition(" ")  # "•"
            lines.append(f"{head} [{dept}] {tail}")
        lines.append("")
        lines.append("Тапни задачу, чтобы посмотреть условия и взять её в работу.")
        text = "\n".join(lines)
        kb = _free_tasks_kb(tasks)

    if cq.message is not None:
        try:
            await cq.message.edit_text(text, reply_markup=kb)
        except Exception as exc:  # noqa: BLE001
            logger.warning("cabinet free: edit failed: {}", exc)
    await cq.answer()


async def _check_free_task_access(task_id: int, user: User) -> tuple[bool, str | None]:
    """Проверка прав на свободную задачу. Возвращает (ok, error_for_user)."""
    if user.role == "admin":
        return True, None
    async with async_session_factory() as session:
        async with session.begin():
            task = await TasksRepository.get_full(session, task_id)
    if task is None:
        return False, "Задача не найдена."
    if user.department_id != task.department_id:
        return False, "Эта задача не из твоего отдела."
    return True, None


async def _render_free_list(
    *, user: User, prefix: str | None = None
) -> tuple[str, InlineKeyboardMarkup]:
    """Перерисовка списка «🎯 Свободные» после действия. prefix — строка,
    которую добавим сверху (например «✅ Задача #N взята в работу.»)."""
    department_id = None if user.role == "admin" else user.department_id
    tasks = await _load_free_tasks(department_id=department_id)
    now_utc = datetime.now(timezone.utc)
    if not tasks:
        head = "🎯 <b>Свободные задачи</b>\n\nБольше свободных задач нет."
        text = f"{prefix}\n\n{head}" if prefix else head
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="⬅️ В кабинет", callback_data="cab:back")]]
        )
        return text, kb
    scope = "по всем отделам" if user.role == "admin" else "в твоём отделе"
    lines = []
    if prefix:
        lines.append(prefix)
        lines.append("")
    lines.append(f"🎯 <b>Свободные задачи</b> ({scope})")
    lines.append("")
    for t in tasks:
        dept = html.escape(t.department.name) if getattr(t, "department", None) else "—"
        base = _task_line(t, now_utc)
        head_part, _sep, tail = base.partition(" ")
        lines.append(f"{head_part} [{dept}] {tail}")
    lines.append("")
    lines.append("Тапни задачу, чтобы посмотреть условия и взять её в работу.")
    return "\n".join(lines), _free_tasks_kb(tasks)


@router.callback_query(StateFilter(CabinetBrowse.viewing), F.data.startswith("cab:free_open:"))
async def cb_free_open(cq: CallbackQuery, state: FSMContext, user: User) -> None:
    """Предпросмотр свободной задачи. Юзер видит полную карточку и
    решает — брать или нет. Принятие — отдельной кнопкой ниже."""
    try:
        task_id = int((cq.data or "").split(":")[2])
    except (ValueError, IndexError):
        await cq.answer("Некорректная задача.", show_alert=True)
        return

    ok, err = await _check_free_task_access(task_id, user)
    if not ok:
        await cq.answer(err or "Нет доступа.", show_alert=True)
        return

    async with async_session_factory() as session:
        async with session.begin():
            task = await TasksRepository.get_full(session, task_id)
            link_chat_id = await AppSettingsRepository.get_int(session, KEY_TASK_CHAT_ID)
            link_topic_id = await AppSettingsRepository.get_int(session, KEY_LEADERSHIP_TOPIC_ID)
    if task is None:
        await cq.answer("Задача не найдена.", show_alert=True)
        return
    if task.status != "new" or task.assignee_id is not None:
        await cq.answer(
            "Задачу уже кто-то взял, пока ты смотрел список.",
            show_alert=True,
        )
        # Перерисуем список — она ушла из свободных.
        text, kb = await _render_free_list(user=user)
        if cq.message is not None:
            try:
                await cq.message.edit_text(text, reply_markup=kb)
            except Exception as exc:  # noqa: BLE001
                logger.debug("free_open refresh after race: {}", exc)
        return

    files_count = len(task.files) if task.files else 0
    card_text = (
        "👁 <b>Предпросмотр свободной задачи</b>\n"
        "Решение — твоё. Берёшь — обязуешься уложиться в срок.\n"
        + "─" * 12
        + "\n"
        + render_task_card(
            task,
            creator=task.creator,
            department=task.department,
            files_count=files_count,
            task_chat_id=link_chat_id,
            lead_topic_id=link_topic_id,
        )
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✋ Принять в работу",
                    callback_data=f"cab:free_accept:{task_id}",
                )
            ],
            [
                InlineKeyboardButton(text="⬅️ К списку", callback_data="cab:free"),
            ],
        ]
    )
    if cq.message is not None:
        try:
            await cq.message.edit_text(card_text, reply_markup=kb)
        except Exception as exc:  # noqa: BLE001
            logger.warning("cabinet free_open edit failed: {}", exc)
    await cq.answer()


@router.callback_query(StateFilter(CabinetBrowse.viewing), F.data.startswith("cab:free_accept:"))
async def cb_free_accept(cq: CallbackQuery, state: FSMContext, bot: Bot, user: User) -> None:
    """Принятие свободной задачи после предпросмотра."""
    from app.services.task_actions import TaskActionsService

    try:
        task_id = int((cq.data or "").split(":")[2])
    except (ValueError, IndexError):
        await cq.answer("Некорректная задача.", show_alert=True)
        return

    ok, err = await _check_free_task_access(task_id, user)
    if not ok:
        await cq.answer(err or "Нет доступа.", show_alert=True)
        return

    result = await TaskActionsService.accept(bot=bot, task_id=task_id, user=user)
    if not result.success:
        await cq.answer(result.user_message, show_alert=True)
        return
    disp_a = result.task.display_number if result.task else task_id
    await cq.answer(f"✅ Взял задачу #{disp_a} в работу.")

    text, kb = await _render_free_list(
        user=user,
        prefix=(
            f"✅ Задача #{disp_a} теперь у тебя в работе. "
            "Карточка с действиями (Завершить / Комментарий / Отменить) — "
            "выше в чате, я её только что обновил."
        ),
    )
    if cq.message is not None:
        try:
            await cq.message.edit_text(text, reply_markup=kb)
        except Exception as exc:  # noqa: BLE001
            logger.warning("cabinet free_accept refresh: edit failed: {}", exc)


@router.callback_query(StateFilter(CabinetBrowse.viewing), F.data == "cab:back")
async def cb_back(cq: CallbackQuery, state: FSMContext, user: User) -> None:
    data = await state.get_data()
    period = data.get(DATA_PERIOD, "week")
    target, prod, buckets, stale = await _load_full(user.id, period)
    body = _format_cabinet(target, prod, buckets, stale, period)
    is_admin = user.role == "admin"
    if cq.message is not None:
        try:
            await cq.message.edit_text(body, reply_markup=_kb(period, is_admin=is_admin))
        except Exception as exc:  # noqa: BLE001
            logger.warning("cabinet back: edit failed: {}", exc)
    await cq.answer()


# ── Этап Г: встроенная карточка задачи в боте ──────────────────────


def _task_menu_kb(tasks: list[Task], user_id: int) -> InlineKeyboardMarkup:
    """Inline-список активных задач юзера. Каждая задача — одна кнопка.

    Помечаем «📨» для задач на согласовании (юзер постановщик ждёт)
    и «🆕» для тех, что юзер поставил и ещё никто не принял.
    """
    rows: list[list[InlineKeyboardButton]] = []
    for t in tasks:
        title = (t.title or "").strip() or "(без названия)"
        if len(title) > 35:
            title = title[:34] + "…"
        if t.creator_id == user_id and t.assignee_id is None:
            marker = "🆕"
        elif t.status == "awaiting_approval":
            marker = "📨"
        elif t.status == "in_progress":
            marker = "🟡"
        else:
            marker = "📋"
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{marker} #{t.display_number} {title}",
                    callback_data=f"cab:task:{t.id}",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="⬅️ В кабинет", callback_data="cab:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(StateFilter(CabinetBrowse.viewing), F.data == "cab:taskmenu")
async def cb_taskmenu(cq: CallbackQuery, state: FSMContext, user: User) -> None:
    """Список активных задач юзера: его исполнительские + его поставленные,
    которые ещё никто не принял (закрывает аудит-нагрузку employee-creator)."""
    async with async_session_factory() as session:
        async with session.begin():
            tasks = list(
                await TasksRepository.list_active_for_user(session, user_id=user.id, limit=50)
            )
    if not tasks:
        text = (
            "🔎 <b>Открыть карточку</b>\n\n"
            "У тебя сейчас нет активных задач — ни на исполнении, "
            "ни поставленных в ожидании приёма."
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="⬅️ В кабинет", callback_data="cab:back")]]
        )
    else:
        own_count = sum(1 for t in tasks if t.creator_id == user.id and t.assignee_id is None)
        body_lines = [
            f"🔎 <b>Активные задачи</b> ({len(tasks)})",
            "",
            "Нажми на задачу, чтобы открыть её карточку прямо в боте — "
            "оттуда же можно завершить, прокомментировать или отменить.",
        ]
        if own_count:
            body_lines.append(
                f"\n🆕 Из них {own_count} — задачи, которые ты поставил и которые "
                "ещё никто не принял."
            )
        text = "\n".join(body_lines)
        kb = _task_menu_kb(tasks, user.id)

    if cq.message is not None:
        try:
            await cq.message.edit_text(text, reply_markup=kb)
        except Exception as exc:  # noqa: BLE001
            logger.warning("cabinet taskmenu: edit failed: {}", exc)
    await cq.answer()


@router.callback_query(StateFilter(CabinetBrowse.viewing), F.data.startswith("cab:task:"))
async def cb_task(cq: CallbackQuery, state: FSMContext, user: User) -> None:
    """Открыть встроенную карточку задачи в боте.

    Проверка доступа: видеть карточку могут creator, assignee, lead, admin.
    Кнопки действий (Завершить / Комментарий / Отменить) рендерятся через
    build_task_card_kb по статусу; права на сами действия проверяются дальше
    в TaskActionsService.
    """
    try:
        task_id = int(cq.data.split(":")[2])
    except (ValueError, IndexError):
        await cq.answer("Некорректная задача.", show_alert=True)
        return

    async with async_session_factory() as session:
        async with session.begin():
            task = await TasksRepository.get_full(session, task_id)
            link_chat_id = await AppSettingsRepository.get_int(session, KEY_TASK_CHAT_ID)
            link_topic_id = await AppSettingsRepository.get_int(session, KEY_LEADERSHIP_TOPIC_ID)
    if task is None:
        await cq.answer("Задача не найдена.", show_alert=True)
        return

    if user.role not in ("admin", "lead") and user.id not in (
        task.creator_id,
        task.assignee_id or -1,
    ):
        await cq.answer("Эта задача не относится к тебе.", show_alert=True)
        return

    files_count = len(task.files) if task.files else 0
    card_text = render_task_card(
        task,
        creator=task.creator,
        assignee=task.assignee,
        department=task.department,
        files_count=files_count,
        task_chat_id=link_chat_id,
        lead_topic_id=link_topic_id,
    )
    action_kb = build_task_card_kb(task.status, task.id)

    # Добавляем «⬅️ К списку» как нижнюю строку клавиатуры действий.
    rows: list[list[InlineKeyboardButton]] = []
    if action_kb:
        rows.extend(action_kb.inline_keyboard)
    # Если задача на согласовании и юзер — постановщик (или админ), даём
    # кнопки приёмки прямо из карточки. Это страховка на случай, когда
    # DM-сообщение с approval-кнопками от complete() потерялось (юзер
    # удалил его, рестарт бота между commit и send_notifications и т.п.).
    # Кнопки согласования — ТОЛЬКО постановщику. Раньше admin тоже видел
    # их (для подстраховки), но это позволяло любому админу закрыть чужую
    # задачу мимо реального заказчика. Теперь approve = только creator.
    if task.status == "awaiting_approval" and task.creator_id == user.id:
        approval_kb = build_approval_kb(task.id)
        rows.extend(approval_kb.inline_keyboard)
    rows.append([InlineKeyboardButton(text="⬅️ К списку задач", callback_data="cab:taskmenu")])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)

    if cq.message is not None:
        try:
            await cq.message.edit_text(card_text, reply_markup=kb)
        except Exception as exc:  # noqa: BLE001
            logger.warning("cabinet task card: edit failed: {}", exc)
    await cq.answer()


@router.callback_query(StateFilter(CabinetBrowse.viewing), F.data == "cab:close")
async def cb_close(cq: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    if cq.message is not None:
        try:
            await cq.message.edit_text("Кабинет закрыт.")
        except Exception:  # noqa: BLE001
            pass
    await cq.answer()
