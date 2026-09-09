"""task_outbox (transactional outbox для публикации в Telegram)

Revision ID: b6c7d8e9f0a1
Revises: a5b6c7d8e9f0
Create Date: 2026-05-18 23:30:00.000000+00:00

Противоотказная публикация задачи в Telegram. INSERT в outbox идёт в
той же транзакции, что и INSERT tasks → даже если бот упадёт после
коммита, фоновый worker дожмёт публикацию по схеме retry.

Бэкфилл: если в базе уже есть задачи без `dept_message_id` / `arch_message_id`
в активных статусах — ставим их в очередь, чтобы дожать. Сейчас таких 0,
но миграция универсальная.

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b6c7d8e9f0a1"
down_revision: Union[str, None] = "a5b6c7d8e9f0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "task_outbox",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "task_id",
            sa.BigInteger(),
            sa.ForeignKey("tasks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column(
            "attempts", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "next_retry_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("NOW()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("NOW()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("NOW()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "action IN ('post_dept','post_lead')",
            name="ck_outbox_action",
        ),
        sa.CheckConstraint(
            "status IN ('pending','done','failed')",
            name="ck_outbox_status",
        ),
    )
    op.create_index(
        "idx_outbox_due",
        "task_outbox",
        ["next_retry_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "uq_outbox_task_action_active",
        "task_outbox",
        ["task_id", "action"],
        unique=True,
        postgresql_where=sa.text("status != 'failed'"),
    )

    # Бэкфилл: задачи в активных статусах без dept_message_id ставим в очередь.
    op.execute(
        """
        INSERT INTO task_outbox (task_id, action, status, next_retry_at)
        SELECT id, 'post_dept', 'pending', NOW()
        FROM tasks
        WHERE dept_message_id IS NULL
          AND status IN ('new','in_progress')
        """
    )
    op.execute(
        """
        INSERT INTO task_outbox (task_id, action, status, next_retry_at)
        SELECT id, 'post_lead', 'pending', NOW()
        FROM tasks
        WHERE arch_message_id IS NULL
          AND status IN ('new','in_progress')
        """
    )


def downgrade() -> None:
    op.drop_index("uq_outbox_task_action_active", table_name="task_outbox")
    op.drop_index("idx_outbox_due", table_name="task_outbox")
    op.drop_table("task_outbox")
