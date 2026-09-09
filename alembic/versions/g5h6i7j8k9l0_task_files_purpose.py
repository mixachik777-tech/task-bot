"""task_files.purpose + outbox update_lead action + pending-only unique

Revision ID: g5h6i7j8k9l0
Revises: bbccddee0102
Create Date: 2026-05-26 11:30:00.000000+00:00

1. task_files.purpose (creation|completion) — отличает файлы постановщика
   от итогового файла исполнителя. Нужно для зеркала в Руководстве и
   админского «Все задачи».

2. task_outbox: добавляем action='update_lead' (редактирование зеркала в
   Руководстве при смене статуса задачи).

3. Меняем partial unique uq_outbox_task_action_active с WHERE status<>'failed'
   на WHERE status='pending'. До этой миграции done-запись блокировала
   постановку нового pending этого же типа — это было ок, пока на задачу
   была единственная публикация. Теперь update_lead ставится много раз
   подряд (accept → complete → approve), и каждая новая попытка должна
   проходить, как только предыдущая зафиналилась.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "g5h6i7j8k9l0"
down_revision: Union[str, None] = "bbccddee0102"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "task_files",
        sa.Column(
            "purpose",
            sa.String(length=16),
            server_default=sa.text("'creation'"),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "ck_task_files_purpose",
        "task_files",
        "purpose IN ('creation','completion')",
    )

    op.add_column(
        "tasks",
        sa.Column(
            "lead_completion_posted",
            sa.Boolean(),
            server_default=sa.text("FALSE"),
            nullable=False,
        ),
    )

    op.drop_constraint("ck_outbox_action", "task_outbox", type_="check")
    op.create_check_constraint(
        "ck_outbox_action",
        "task_outbox",
        "action IN ('post_dept','post_lead','broadcast','update_lead')",
    )

    op.execute("DROP INDEX IF EXISTS uq_outbox_task_action_active")
    op.execute(
        "CREATE UNIQUE INDEX uq_outbox_task_action_active "
        "ON task_outbox (task_id, action) "
        "WHERE status = 'pending'"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_outbox_task_action_active")
    op.execute(
        "CREATE UNIQUE INDEX uq_outbox_task_action_active "
        "ON task_outbox (task_id, action) "
        "WHERE status <> 'failed'"
    )

    op.drop_constraint("ck_outbox_action", "task_outbox", type_="check")
    op.create_check_constraint(
        "ck_outbox_action",
        "task_outbox",
        "action IN ('post_dept','post_lead','broadcast')",
    )

    op.drop_column("tasks", "lead_completion_posted")

    op.drop_constraint("ck_task_files_purpose", "task_files", type_="check")
    op.drop_column("task_files", "purpose")
