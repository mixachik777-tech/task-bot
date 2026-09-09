"""tasks.overdue_message_id

Revision ID: i7j8k9l0m1n2
Revises: h6i7j8k9l0m1
Create Date: 2026-05-27 17:10:00.000000+00:00

Колонка для message_id карточки задачи в топике «Просроченные» (Этап Д).
NULL — задача либо никогда не была просрочена, либо overdue_topic_id ещё
не настроен. При закрытии задачи (approve/cancel/complete) сообщение в
overdue-топике удаляется и колонка сбрасывается обратно в NULL.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "i7j8k9l0m1n2"
down_revision: Union[str, None] = "h6i7j8k9l0m1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column("overdue_message_id", sa.BigInteger(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("tasks", "overdue_message_id")
