"""tasks.display_number — сквозная плотная нумерация без дыр

Revision ID: k9l0m1n2o3p4
Revises: j8k9l0m1n2o3
Create Date: 2026-05-28 13:30:00.000000+00:00

Внутренний `tasks.id` остаётся первичным ключом и используется во всех
FK (task_history, task_files, task_broadcasts, task_outbox). Однако
`tasks_id_seq` прожигает значения при rollback/delete, что оставляет
видимые пользователю дыры в нумерации задач. Это путает админа и
выглядит как баг данных.

Колонка `display_number` — отдельная плотная нумерация для UI:
- Бэкфилл существующих задач: ROW_NUMBER() OVER (ORDER BY id) →
  старые задачи получают 1..N подряд.
- Новые задачи получают MAX(display_number)+1 при создании
  (через TasksRepository, не через DEFAULT — нужен SELECT FOR UPDATE
  для защиты от гонок без отдельной sequence).
- UNIQUE гарантирует, что номер не повторится.
- NOT NULL — все задачи всегда имеют человеческий номер.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "k9l0m1n2o3p4"
down_revision: Union[str, None] = "j8k9l0m1n2o3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1) добавляем колонку как nullable, чтобы заполнить bcakfill'ом
    op.add_column(
        "tasks",
        sa.Column("display_number", sa.BigInteger(), nullable=True),
    )
    # 2) backfill: плотная нумерация 1..N по возрастанию id
    op.execute(
        """
        WITH numbered AS (
            SELECT id, ROW_NUMBER() OVER (ORDER BY id) AS rn FROM tasks
        )
        UPDATE tasks t SET display_number = n.rn
        FROM numbered n WHERE t.id = n.id
        """
    )
    # 3) делаем NOT NULL и уникальной
    op.alter_column("tasks", "display_number", nullable=False)
    op.create_unique_constraint("uq_tasks_display_number", "tasks", ["display_number"])
    # 4) индекс — для быстрого MAX и поиска по человеческому номеру
    op.create_index("ix_tasks_display_number", "tasks", ["display_number"])


def downgrade() -> None:
    op.drop_index("ix_tasks_display_number", table_name="tasks")
    op.drop_constraint("uq_tasks_display_number", "tasks", type_="unique")
    op.drop_column("tasks", "display_number")
