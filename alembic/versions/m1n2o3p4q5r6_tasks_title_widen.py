"""tasks.title — расширение до VARCHAR(1024)

Revision ID: m1n2o3p4q5r6
Revises: l0m1n2o3p4q5
Create Date: 2026-06-02 07:45:00.000000+00:00

VARCHAR(255) узко для длинных названий задач (особенно вставленных
пользователем из других источников). Поднимаем до 1024 — этого хватит
с запасом, при этом в карточке задачи в TG-сообщении вмещается без
урезания. ALTER на VARCHAR-длине мгновенен и не блокирует таблицу.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "m1n2o3p4q5r6"
down_revision: Union[str, None] = "l0m1n2o3p4q5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "tasks",
        "title",
        existing_type=sa.String(length=255),
        type_=sa.String(length=1024),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "tasks",
        "title",
        existing_type=sa.String(length=1024),
        type_=sa.String(length=255),
        existing_nullable=False,
    )
