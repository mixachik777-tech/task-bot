"""
Перечисления и связанные таблицы маппинга для доменной модели задач.

В коде используются как типизированные enum'ы; в БД хранятся как VARCHAR
(валидация — через CHECK constraint на уровне таблицы). PgEnum намеренно
не используется — миграция значений в нём болезненна.
"""

from datetime import timedelta
from enum import Enum


class TaskPriority(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    URGENT = "urgent"


class TaskStatus(str, Enum):
    NEW = "new"
    IN_PROGRESS = "in_progress"
    # Этап Б «Согласование постановщиком»: исполнитель отправил результат,
    # ждём решения creator'а (или admin'а) — «Согласовано» / «На доработку».
    AWAITING_APPROVAL = "awaiting_approval"
    DONE = "done"
    CANCELLED = "cancelled"


class UserRole(str, Enum):
    EMPLOYEE = "employee"
    LEAD = "lead"
    ADMIN = "admin"


class AccessStatus(str, Enum):
    """Статус доступа пользователя к боту.

    pending  — нажал /start, ждёт решения аппрувера
    approved — одобрен, is_active=True
    denied   — отклонён (или soft-deleted), is_active=False
    """

    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"


class HistoryEventType(str, Enum):
    CREATED = "created"
    ACCEPTED = "accepted"
    # Исполнитель отправил отчёт и попросил согласование (Этап Б).
    SUBMITTED_FOR_APPROVAL = "submitted_for_approval"
    # Постановщик согласовал результат — задача закрывается.
    APPROVED = "approved"
    # Постановщик вернул на доработку (см. Этап В для обязательного комментария).
    REWORK_REQUESTED = "rework_requested"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    COMMENTED = "commented"
    REASSIGNED = "reassigned"
    DEADLINE_CHANGED = "deadline_changed"
    # Исполнитель задал уточняющий вопрос постановщику (Этап Г).
    QUESTION_ASKED = "question_asked"
    # Постановщик ответил на вопрос исполнителя.
    QUESTION_ANSWERED = "question_answered"


class FileKind(str, Enum):
    """Тип Telegram-вложения. Нужен, чтобы при отправке файла в чат
    выбрать правильный метод API: send_photo / send_video / send_animation
    / send_document. file_id у разных типов несовместимы."""

    PHOTO = "photo"
    DOCUMENT = "document"
    VIDEO = "video"
    ANIMATION = "animation"


class ReminderType(str, Enum):
    """Типы напоминаний планировщика. Маппинг на (имя_флага, threshold) — REMINDER_CONFIG."""

    H2 = "h2"
    D1 = "d1"
    D2 = "d2"


REMINDER_CONFIG: dict[ReminderType, tuple[str, timedelta]] = {
    ReminderType.H2: ("reminder_2h_sent", timedelta(hours=2)),
    ReminderType.D1: ("reminder_1d_sent", timedelta(days=1)),
    ReminderType.D2: ("reminder_2d_sent", timedelta(days=2)),
}


ALLOWED_STATUS_TRANSITIONS: dict[tuple[TaskStatus, TaskStatus], HistoryEventType] = {
    (TaskStatus.NEW, TaskStatus.IN_PROGRESS): HistoryEventType.ACCEPTED,
    (TaskStatus.NEW, TaskStatus.CANCELLED): HistoryEventType.CANCELLED,
    # Этап Б: «✅ Завершить» исполнителя теперь не закрывает задачу,
    # а отправляет на согласование постановщику.
    (TaskStatus.IN_PROGRESS, TaskStatus.AWAITING_APPROVAL): (
        HistoryEventType.SUBMITTED_FOR_APPROVAL
    ),
    (TaskStatus.IN_PROGRESS, TaskStatus.CANCELLED): HistoryEventType.CANCELLED,
    # «✅ Согласовано» — задача закрывается.
    (TaskStatus.AWAITING_APPROVAL, TaskStatus.DONE): HistoryEventType.APPROVED,
    # «✏️ На доработку» — задача снова у исполнителя.
    (TaskStatus.AWAITING_APPROVAL, TaskStatus.IN_PROGRESS): (HistoryEventType.REWORK_REQUESTED),
    # Отмена возможна на любой нетерминальной стадии.
    (TaskStatus.AWAITING_APPROVAL, TaskStatus.CANCELLED): HistoryEventType.CANCELLED,
}


TERMINAL_STATUSES: frozenset[TaskStatus] = frozenset({TaskStatus.DONE, TaskStatus.CANCELLED})
