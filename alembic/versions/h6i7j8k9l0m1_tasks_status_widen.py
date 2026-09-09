"""tasks.status VARCHAR(16) → VARCHAR(32)

Revision ID: h6i7j8k9l0m1
Revises: g5h6i7j8k9l0
Create Date: 2026-05-26 11:50:00.000000+00:00

Колонка tasks.status была объявлена VARCHAR(16) ещё на первой схеме,
когда было 4 статуса (new/in_progress/done/cancelled, максимум 11
символов). Этап Б добавил статус 'awaiting_approval' (17 символов) —
ck_tasks_status был обновлён в bbccddee0102, но саму длину типа
там не подняли. На первом complete упало:
  StringDataRightTruncationError: value too long for type character varying(16)

Расширяем до VARCHAR(32) с запасом на будущие статусы.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "h6i7j8k9l0m1"
down_revision: Union[str, None] = "g5h6i7j8k9l0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "tasks",
        "status",
        existing_type=sa.String(length=16),
        type_=sa.String(length=32),
        existing_nullable=False,
        existing_server_default=sa.text("'new'"),
    )


def downgrade() -> None:
    op.alter_column(
        "tasks",
        "status",
        existing_type=sa.String(length=32),
        type_=sa.String(length=16),
        existing_nullable=False,
        existing_server_default=sa.text("'new'"),
    )
