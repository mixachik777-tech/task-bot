"""
SQLAlchemy 2.0 модели доменной схемы task-bot.

Все enum-подобные поля (status, priority, role, event_type) хранятся как
VARCHAR + CHECK constraint. Не PgEnum — миграция значений болезненна.
JSONB-поля — через sqlalchemy.dialects.postgresql.JSONB.
TIMESTAMPTZ — через DateTime(timezone=True).
"""

from datetime import datetime
from typing import Any, List, Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class Department(Base):
    __tablename__ = "departments"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    topic_id: Mapped[int] = mapped_column(Integer, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("TRUE"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    users: Mapped[List["User"]] = relationship(back_populates="department")
    tasks: Mapped[List["Task"]] = relationship(back_populates="department")


class User(Base):
    """
    Сотрудник редакции.

    `updated_at`: onupdate=func.now() работает только через ORM (на UPDATE
    через ORM-сессию). При сыром SQL обновляй поле вручную.
    """

    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(
            "role IN ('employee','lead','admin')",
            name="ck_users_role",
        ),
        CheckConstraint(
            "access_status IN ('pending','approved','denied')",
            name="ck_users_access_status",
        ),
        Index("idx_users_department_id", "department_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tg_user_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    tg_username: Mapped[Optional[str]] = mapped_column(String(64))
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(32), server_default=text("'employee'"), nullable=False)
    department_id: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("departments.id"))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("TRUE"), nullable=False)
    access_status: Mapped[str] = mapped_column(
        String(16), server_default=text("'pending'"), nullable=False
    )
    notified_admins_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    department: Mapped[Optional["Department"]] = relationship(back_populates="users")
    created_tasks: Mapped[List["Task"]] = relationship(
        back_populates="creator",
        foreign_keys="Task.creator_id",
    )
    assigned_tasks: Mapped[List["Task"]] = relationship(
        back_populates="assignee",
        foreign_keys="Task.assignee_id",
    )
    ai_conversations: Mapped[List["AiConversation"]] = relationship(back_populates="user")


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        CheckConstraint(
            "priority IN ('low','medium','high','urgent')",
            name="ck_tasks_priority",
        ),
        CheckConstraint(
            "status IN ('new','in_progress','awaiting_approval','done','cancelled')",
            name="ck_tasks_status",
        ),
        Index("idx_tasks_status", "status"),
        Index("idx_tasks_assignee", "assignee_id"),
        Index("idx_tasks_creator", "creator_id"),
        Index("idx_tasks_dept_status", "department_id", "status"),
        Index(
            "idx_tasks_deadline",
            "deadline",
            postgresql_where=text("status IN ('new','in_progress')"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # display_number — человеческий сквозной номер задачи. id остаётся
    # внутренним PK для FK, но пользователь во всех текстах и кнопках
    # видит только display_number. Заполняется отдельной функцией
    # TasksRepository.next_display_number, не через sequence — чтобы
    # не было дыр от rollback'ов/тестовых вставок (см. миграцию
    # k9l0m1n2o3p4).
    display_number: Mapped[int] = mapped_column(BigInteger, nullable=False, unique=True)
    title: Mapped[str] = mapped_column(String(1024), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)
    priority: Mapped[str] = mapped_column(String(16), nullable=False)
    deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(32), server_default=text("'new'"), nullable=False)
    creator_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
    assignee_id: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("users.id"))
    department_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("departments.id"), nullable=False
    )
    dept_chat_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    dept_message_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    arch_message_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    overdue_message_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    accepted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    reminder_2d_sent: Mapped[bool] = mapped_column(
        Boolean, server_default=text("FALSE"), nullable=False
    )
    reminder_1d_sent: Mapped[bool] = mapped_column(
        Boolean, server_default=text("FALSE"), nullable=False
    )
    reminder_2h_sent: Mapped[bool] = mapped_column(
        Boolean, server_default=text("FALSE"), nullable=False
    )
    is_message_purged: Mapped[bool] = mapped_column(
        Boolean, server_default=text("FALSE"), nullable=False
    )
    lead_completion_posted: Mapped[bool] = mapped_column(
        Boolean, server_default=text("FALSE"), nullable=False
    )

    department: Mapped["Department"] = relationship(back_populates="tasks")
    creator: Mapped["User"] = relationship(
        back_populates="created_tasks",
        foreign_keys=[creator_id],
    )
    assignee: Mapped[Optional["User"]] = relationship(
        back_populates="assigned_tasks",
        foreign_keys=[assignee_id],
    )
    files: Mapped[List["TaskFile"]] = relationship(
        back_populates="task",
        cascade="all, delete-orphan",
    )
    history: Mapped[List["TaskHistory"]] = relationship(
        back_populates="task",
        cascade="all, delete-orphan",
    )


class TaskFile(Base):
    __tablename__ = "task_files"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('photo','document','video','animation')",
            name="ck_task_files_kind",
        ),
        CheckConstraint(
            "purpose IN ('creation','completion')",
            name="ck_task_files_purpose",
        ),
        Index("idx_task_files_task_id", "task_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    task_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
    )
    tg_file_id: Mapped[str] = mapped_column(String(255), nullable=False)
    tg_file_unique_id: Mapped[Optional[str]] = mapped_column(String(255))
    file_name: Mapped[Optional[str]] = mapped_column(String(255))
    file_size: Mapped[Optional[int]] = mapped_column(Integer)
    mime_type: Mapped[Optional[str]] = mapped_column(String(128))
    kind: Mapped[str] = mapped_column(String(16), server_default=text("'document'"), nullable=False)
    purpose: Mapped[str] = mapped_column(
        String(16), server_default=text("'creation'"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    task: Mapped["Task"] = relationship(back_populates="files")


class TaskHistory(Base):
    __tablename__ = "task_history"
    __table_args__ = (
        CheckConstraint(
            "event_type IN ('created','accepted','submitted_for_approval',"
            "'approved','rework_requested','completed','cancelled',"
            "'commented','reassigned','deadline_changed')",
            name="ck_task_history_event_type",
        ),
        Index("idx_history_task", "task_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    task_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("users.id"))
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    task: Mapped["Task"] = relationship(back_populates="history")
    user: Mapped[Optional["User"]] = relationship()


class TaskOutbox(Base):
    """
    Transactional outbox: задание на публикацию карточки задачи в Telegram.

    INSERT в эту таблицу делается в той же транзакции, что и сам INSERT
    `tasks`. После commit'а — outbox-worker (синхронно сразу + фоновый job
    каждую минуту) пробует доставить публикацию с экспоненциальным retry.
    После N попыток (см. `OutboxService.RETRY_SCHEDULE`) ставится `failed`
    и админу системы уходит DM-алерт. Никакая задача не «зависает» в БД
    без публикации молча.

    `action`:
      post_dept — основная карточка в топик отдела (с кнопками действий)
      post_lead — зеркало в топик «Руководство» (без кнопок)

    Уникальность (task_id, action) среди не-`failed` записей гарантирует,
    что для одной задачи не появится дублирующая публикация.
    """

    __tablename__ = "task_outbox"
    __table_args__ = (
        CheckConstraint(
            "action IN ('post_dept','post_lead','broadcast')",
            name="ck_outbox_action",
        ),
        CheckConstraint(
            "status IN ('pending','done','failed')",
            name="ck_outbox_status",
        ),
        Index(
            "idx_outbox_due",
            "next_retry_at",
            postgresql_where=text("status = 'pending'"),
        ),
        Index(
            "uq_outbox_task_action_active",
            "task_id",
            "action",
            unique=True,
            postgresql_where=text("status != 'failed'"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    task_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
    )
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), server_default=text("'pending'"), nullable=False
    )
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"), nullable=False)
    last_error: Mapped[Optional[str]] = mapped_column(Text)
    next_retry_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class AppSetting(Base):
    """
    Key-value хранилище глобальных настроек: TASK_CHAT_ID,
    LEADERSHIP_TOPIC_ID и т.п. Значение — всегда строка
    (числовые приводятся в репозитории).
    """

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class AiConversation(Base):
    """
    История диалога пользователя с AI-агентом.

    `updated_at`: onupdate=func.now() работает только через ORM (на UPDATE
    через ORM-сессию). При сыром SQL обновляй поле вручную.

    Индекс idx_ai_conversations_user_updated (user_id, updated_at DESC) —
    под основной запрос Этапа 8 «последний разговор пользователя».
    """

    __tablename__ = "ai_conversations"
    __table_args__ = (
        Index(
            "idx_ai_conversations_user_updated",
            "user_id",
            text("updated_at DESC"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
    messages: Mapped[list[Any]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    user: Mapped["User"] = relationship(back_populates="ai_conversations")


class TaskBroadcast(Base):
    """
    Доставленная карточка задачи в личку конкретному пользователю.

    Этап А «Бот без чата»: при создании задачи бот рассылает карточку
    каждому активному участнику направления в личку и фиксирует здесь
    (task_id, user_id, message_id). При принятии задачи кем-то из них
    остальные записи используются чтобы удалить карточки у всех, кроме
    принявшего, через bot.delete_message.

    UNIQUE (task_id, user_id) гарантирует идемпотентность повторного
    broadcast-attempt'а — outbox-worker не будет слать дубль тому же
    юзеру, если предыдущая попытка частично проскочила до сбоя.
    """

    __tablename__ = "task_broadcasts"
    __table_args__ = (Index("idx_task_broadcasts_task", "task_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    task_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
    # tg_user_id денормализован: при delete_message надо chat_id (=tg_user_id),
    # JOIN на users ради каждого клика «Принять» — лишнее.
    tg_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    message_id: Mapped[int] = mapped_column(Integer, nullable=False)
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
