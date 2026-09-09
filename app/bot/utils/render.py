"""
Рендеринг карточек задач для топиков отделов и зеркала «Руководство».

HTML-парс-мод (бот стартует с ParseMode.HTML), поэтому пользовательские
строки экранируются через html.escape().
"""

import html
from datetime import datetime, timezone
from typing import Mapping, Sequence

from app.bot.utils.tg import build_topic_message_link
from app.bot.utils.time import format_delta, format_dt_local, now_utc
from app.db.models import Department, Task, TaskHistory, User


def _build_origin_link(
    task: Task,
    *,
    task_chat_id: int | None,
    lead_topic_id: int | None,
) -> str | None:
    """Ссылка на оригинал задачи (зеркало в «Руководстве»).

    Возвращает HTML-строку «🔗 Открыть задачу» или None, если нет данных
    (не настроены lead-topic / task-chat в app_settings, либо задача ещё
    не зеркалирована).
    """
    if not task_chat_id or not lead_topic_id or not task.arch_message_id:
        return None
    link = build_topic_message_link(task_chat_id, lead_topic_id, task.arch_message_id)
    if not link:
        return None
    return f'🔗 <a href="{link}">Открыть задачу</a>'


STATUS_EMOJI: dict[str, str] = {
    "new": "🆕",
    "in_progress": "🟡",
    "awaiting_approval": "📨",
    "done": "✅",
    "cancelled": "❌",
}
STATUS_NAME: dict[str, str] = {
    "new": "Новая",
    "in_progress": "В работе",
    "awaiting_approval": "На согласовании",
    "done": "Выполнено",
    "cancelled": "Отменена",
}
PRIORITY_EMOJI: dict[str, str] = {
    "low": "🟢",
    "medium": "🟡",
    "high": "🟠",
    "urgent": "🔴",
}
PRIORITY_NAME: dict[str, str] = {
    "low": "Низкая",
    "medium": "Средняя",
    "high": "Высокая",
    "urgent": "Срочная",
}


def _user_handle(user: User | None) -> str:
    if user is None:
        return "неизвестен"
    if user.tg_username:
        return f"@{html.escape(user.tg_username)}"
    return html.escape(user.full_name or f"id={user.tg_user_id}")


def render_task_card(
    task: Task,
    *,
    creator: User,
    assignee: User | None = None,
    department: Department | None = None,
    files_count: int = 0,
    now: datetime | None = None,
    task_chat_id: int | None = None,
    lead_topic_id: int | None = None,
) -> str:
    """
    Карточка задачи для топика.

    Для активных задач (new/in_progress) с просроченным deadline статусная
    строка переключается на «🔴 Просрочена» — визуальный сигнал
    в дополнение к overdue-DM от планировщика.

    Если переданы task_chat_id + lead_topic_id и у задачи есть
    arch_message_id — в конец добавляется строка-ссылка «🔗 Открыть
    задачу» (зеркало в «Руководстве»). Используется в местах, где
    карточка показывается как копия (кабинет, архив, overdue-топик).
    Сами оригиналы (dept-карточка, lead-зеркало) ссылку не передают.
    """
    now_ = now if now is not None else now_utc()
    deadline_local = format_dt_local(task.deadline)
    delta_seconds = int((task.deadline - now_).total_seconds())
    delta_str = format_delta(delta_seconds)
    is_active = task.status in ("new", "in_progress")
    is_overdue = is_active and task.deadline < now_

    lines: list[str] = [
        f"{STATUS_EMOJI[task.status]} <b>Задача #{task.display_number}</b>",
        f"📌 {html.escape(task.title)}",
        f"{PRIORITY_EMOJI[task.priority]} Важность: {PRIORITY_NAME[task.priority]}",
        f"⏰ Срок: {deadline_local} ({delta_str})",
        f"👤 Постановщик: {_user_handle(creator)}",
    ]
    if department is not None:
        lines.append(f"🏢 Отдел: {html.escape(department.name)}")
    if task.description:
        if "\n" in task.description:
            lines.append("📝 Описание:")
            for ln in task.description.split("\n"):
                lines.append(html.escape(ln))
        else:
            lines.append(f"📝 Описание: {html.escape(task.description)}")
    if files_count:
        lines.append(f"📎 Файлов: {files_count}")
    lines.append("─" * 12)

    if is_overdue:
        if task.status == "in_progress" and assignee is not None:
            lines.append(f"Статус: 🔴 Просрочена — {_user_handle(assignee)}")
        else:
            lines.append("Статус: 🔴 Просрочена")
    elif task.status == "in_progress" and assignee is not None:
        lines.append(f"Статус: 🟡 В работе — {_user_handle(assignee)}")
    elif task.status == "awaiting_approval" and assignee is not None:
        lines.append(
            f"Статус: 📨 На согласовании у постановщика (выполнил {_user_handle(assignee)})"
        )
    elif task.status == "awaiting_approval":
        lines.append("Статус: 📨 На согласовании у постановщика")
    elif task.status == "done":
        lines.append("Статус: ✅ Выполнено")
    elif task.status == "cancelled":
        lines.append("Статус: ❌ Отменена")
    else:
        lines.append("Статус: 🆕 Новая")

    origin_link = _build_origin_link(task, task_chat_id=task_chat_id, lead_topic_id=lead_topic_id)
    if origin_link:
        lines.append(origin_link)

    return "\n".join(lines)


EVENT_LABEL: dict[str, str] = {
    "created": "🆕 Создана",
    "accepted": "✋ Принята в работу",
    "submitted_for_approval": "📨 Отправлена на согласование",
    "approved": "✅ Согласована",
    "rework_requested": "✏️ Возвращена на доработку",
    "completed": "✅ Завершена",
    "cancelled": "❌ Отменена",
    "commented": "💬 Комментарий",
    "reassigned": "🔄 Переназначена",
    "deadline_changed": "⏰ Срок изменён",
    "question_asked": "❓ Вопрос исполнителя",
    "question_answered": "💬 Ответ постановщика",
}

ROLE_RU: dict[str, str] = {
    "creator": "создатель",
    "assignee": "исполнитель",
    "admin": "админ",
    "lead": "руководитель",
    "employee": "сотрудник",
}


def _payload_suffix(event_type: str, payload: dict | None) -> str:
    """Локализованный хвост события: комментарий, причина, роль и т.п."""
    if not payload:
        return ""
    if event_type in ("completed", "submitted_for_approval"):
        result = payload.get("result_comment")
        if result:
            return f" — «{html.escape(str(result))}»"
        return ""
    if event_type == "rework_requested":
        comment = payload.get("comment")
        if comment:
            return f" — «{html.escape(str(comment))}»"
        return ""
    if event_type == "cancelled":
        by_role = payload.get("by_role")
        if by_role:
            return f" ({ROLE_RU.get(by_role, by_role)})"
        return ""
    if event_type == "commented":
        text = payload.get("text", "")
        from_role = payload.get("from_user_role")
        role_part = f" ({ROLE_RU.get(from_role, from_role)})" if from_role else ""
        return f"{role_part}: «{html.escape(str(text))}»"
    if event_type in ("question_asked", "question_answered"):
        text = payload.get("text", "")
        if text:
            return f": «{html.escape(str(text))}»"
        return ""
    if event_type == "reassigned":
        # payload: {from_user_id, to_user_id, by_role}
        return ""
    return ""


def render_task_history(
    events: Sequence[TaskHistory],
    *,
    users_by_id: Mapping[int, User] | None = None,
) -> str:
    """
    Текстовая лента событий задачи. Формат строки:
    `DD.MM.YYYY HH:MM — <event_label> [— <user>] [<payload_suffix>]`.

    Сортировка не выполняется — приходит уже упорядоченным из репозитория
    (created_at ASC, id ASC).
    """
    if not events:
        return "—"
    lines: list[str] = []
    for ev in events:
        when = format_dt_local(ev.created_at)
        label = EVENT_LABEL.get(ev.event_type, ev.event_type)
        user = (
            users_by_id.get(ev.user_id)
            if (users_by_id is not None and ev.user_id is not None)
            else None
        )
        user_part = f" — {_user_handle(user)}" if user is not None else ""
        suffix = _payload_suffix(ev.event_type, ev.payload)
        lines.append(f"{when} — {label}{user_part}{suffix}")
    return "\n".join(lines)


def render_archive_row(task: Task) -> str:
    """
    Одна строка списка архива: «#42 17.05 14:32 ✅ Сверстать обложку».
    Без HTML — пойдёт как текст кнопки или строка списка.
    """
    when_src = task.completed_at or task.created_at
    if when_src.tzinfo is None:
        when_src = when_src.replace(tzinfo=timezone.utc)
    when = format_dt_local(when_src)[:-6]  # без года: «17.05 14:32»
    emoji = STATUS_EMOJI.get(task.status, "•")
    title = task.title if len(task.title) <= 36 else task.title[:33] + "…"
    return f"#{task.display_number} {when} {emoji} {title}"


def render_preview(
    *,
    title: str,
    department_name: str,
    priority: str,
    deadline_utc: datetime,
    description: str | None,
    files_count: int,
) -> str:
    parts: list[str] = [
        "<b>Предпросмотр задачи:</b>",
        "",
        f"📌 {html.escape(title)}",
        f"🏢 Отдел: {html.escape(department_name)}",
        f"{PRIORITY_EMOJI[priority]} Важность: {PRIORITY_NAME[priority]}",
        f"⏰ Срок: {format_dt_local(deadline_utc)}",
    ]
    if description:
        if "\n" in description:
            parts.append("📝 Описание:")
            for ln in description.split("\n"):
                parts.append(html.escape(ln))
        else:
            parts.append(f"📝 Описание: {html.escape(description)}")
    if files_count:
        parts.append(f"📎 Файлов: {files_count}")
    return "\n".join(parts)
