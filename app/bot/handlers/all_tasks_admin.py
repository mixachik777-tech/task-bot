"""
Раздел «📑 Все задачи» в личном кабинете админа.

Доступ — только UserRole.ADMIN. Точка входа — inline-кнопка
«📑 Все задачи (админ)» внутри «👤 Кабинет» (cabinet.py добавляет её
только для admin).

Карточка задачи в списке:
  #ID «название»  [статус]
  От: <postavshik>
  Отдел: <название>
  Принял: <assignee>, DD.MM HH:MM
  Завершено: DD.MM HH:MM
  Итог: 📎 <file_name>
  [📂 Открыть в Руководстве]

Полный оригинал (плюс файлы) — в топике «Руководство» (закрытый),
ссылка ведёт прямо на сообщение зеркала по arch_message_id. Если зеркала
нет (старая задача или Lead-топик не настроен) — кнопка не показывается.

Пагинация — PAGE_SIZE задач на страницу, навигация «◀️/▶️» в нижнем ряду.
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

router = Router(name="all_tasks_admin")

PAGE_SIZE = 5
CB_PREFIX = "at:page:"


def _short_handle(user: User | None) -> str:
    if user is None:
        return "—"
    if user.tg_username:
        return f"@{html.escape(user.tg_username)}"
    return html.escape(user.full_name or f"id={user.tg_user_id}")


def _completion_file_label(task: Task) -> str | None:
    """Имя итогового файла (purpose='completion'), если есть."""
    if not task.files:
        return None
    for f in task.files:
        if getattr(f, "purpose", "creation") == "completion":
            name = f.file_name or {
                "photo": "фото",
                "video": "видео",
                "animation": "анимация",
                "document": "файл",
            }.get(f.kind, "файл")
            return html.escape(str(name))
    return None


def _render_task_block(task: Task) -> str:
    title = (task.title or "").strip() or "(без названия)"
    if len(title) > 60:
        title = title[:57] + "…"
    emoji = STATUS_EMOJI.get(task.status, "•")
    status_name = STATUS_NAME.get(task.status, task.status)
    lines: list[str] = [
        f"<b>#{task.display_number}</b> «{html.escape(title)}»  [{emoji} {status_name}]",
        f"От: {_short_handle(task.creator)}",
    ]
    dept_name = task.department.name if task.department else "—"
    lines.append(f"Отдел: {html.escape(dept_name)}")

    if task.assignee is not None and task.accepted_at is not None:
        lines.append(f"Принял: {_short_handle(task.assignee)}, {format_dt_local(task.accepted_at)}")
    elif task.assignee is not None:
        lines.append(f"Принял: {_short_handle(task.assignee)}")
    else:
        lines.append("Принял: —")

    if task.completed_at is not None:
        lines.append(f"Завершено: {format_dt_local(task.completed_at)}")
    else:
        lines.append("Завершено: —")

    label = _completion_file_label(task)
    if label is not None:
        lines.append(f"Итог: 📎 {label}")
    else:
        lines.append("Итог: —")

    return "\n".join(lines)


def _render_header(page: int, total: int) -> str:
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page_human = page + 1
    return (
        f"📑 <b>Все задачи</b>  (страница {page_human} из {pages}, всего {total})\n"
        f"Полный оригинал и файлы — в закрытом топике «Руководство»."
    )


def _build_kb(
    page: int,
    total: int,
    tasks_on_page: list[Task],
    *,
    task_chat_id: int,
    lead_topic_id: int,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for t in tasks_on_page:
        if t.arch_message_id and task_chat_id and lead_topic_id:
            link = build_topic_message_link(task_chat_id, lead_topic_id, t.arch_message_id)
            if link:
                # Кнопка называется по названию задачи (а не по #ID), чтобы
                # глаз быстро находил нужную в длинном списке. ID остаётся
                # в текстовом блоке сверху для однозначности.
                title = (t.title or "").strip() or f"#{t.display_number}"
                # Telegram режет inline-кнопки на ~64 байтах; обрезаем
                # с запасом, чтобы префикс «📂 » и многоточие тоже вошли.
                if len(title) > 40:
                    title = title[:39] + "…"
                rows.append(
                    [
                        InlineKeyboardButton(
                            text=f"📂 {title}",
                            url=link,
                        )
                    ]
                )
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"{CB_PREFIX}{page - 1}"))
    if (page + 1) * PAGE_SIZE < total:
        nav.append(InlineKeyboardButton(text="Вперёд ▶️", callback_data=f"{CB_PREFIX}{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="✖️ Закрыть", callback_data="at:close")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _render_page(
    page: int,
) -> tuple[str, InlineKeyboardMarkup]:
    async with async_session_factory() as session:
        async with session.begin():
            tasks, total = await TasksRepository.list_for_admin_archive(
                session, limit=PAGE_SIZE, offset=page * PAGE_SIZE
            )
            task_chat_id = (await AppSettingsRepository.get_int(session, KEY_TASK_CHAT_ID)) or 0
            lead_topic_id = (
                await AppSettingsRepository.get_int(session, KEY_LEADERSHIP_TOPIC_ID)
            ) or 0
    tasks_list = list(tasks)
    if not tasks_list:
        text = "📑 <b>Все задачи</b>\n\nПока ни одной задачи в базе."
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="✖️ Закрыть", callback_data="at:close")]]
        )
        return text, kb
    blocks = [_render_header(page, total)] + [_render_task_block(t) for t in tasks_list]
    text = "\n\n".join(blocks)
    kb = _build_kb(
        page,
        total,
        tasks_list,
        task_chat_id=task_chat_id,
        lead_topic_id=lead_topic_id,
    )
    return text, kb


def _is_admin(user: User | None) -> bool:
    return user is not None and user.role == UserRole.ADMIN.value and user.is_active


@router.callback_query(F.data.startswith(CB_PREFIX))
async def cb_alltasks_page(cq: CallbackQuery, user: User | None = None) -> None:
    if not _is_admin(user):
        await cq.answer("Раздел только для админа.", show_alert=True)
        return
    try:
        page = int((cq.data or "").split(":")[2])
    except (ValueError, IndexError):
        await cq.answer("Некорректная страница.", show_alert=True)
        return
    if page < 0:
        page = 0
    text, kb = await _render_page(page)
    if cq.message is not None:
        try:
            await cq.message.edit_text(text, reply_markup=kb)
        except Exception as exc:  # noqa: BLE001
            logger.debug("all_tasks page edit failed: {}", exc)
    await cq.answer()


@router.callback_query(F.data == "at:close")
async def cb_alltasks_close(cq: CallbackQuery, user: User | None = None) -> None:
    closed_ok = False
    if cq.message is not None:
        try:
            await cq.message.edit_text("Раздел «Все задачи» закрыт.")
            closed_ok = True
        except Exception as exc:  # noqa: BLE001
            logger.debug("alltasks close edit failed: {}", exc)
    # Fallback: даём явный toast, даже если edit упал — иначе юзер видит
    # пустой ответ и не понимает, закрылся ли раздел.
    await cq.answer("Закрыто" if closed_ok else "Раздел закрыт.")
