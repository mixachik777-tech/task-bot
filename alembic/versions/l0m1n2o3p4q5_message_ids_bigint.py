"""tasks.dept_message_id и tasks.arch_message_id — BigInteger

Revision ID: l0m1n2o3p4q5
Revises: k9l0m1n2o3p4
Create Date: 2026-06-01 13:55:00.000000+00:00

Telegram message_id для активной супергруппы со временем превышает
2_147_483_647 (предел signed int32 → колонка Integer). Тогда любой
INSERT/UPDATE с большим id падает с OutOfRangeError. Колонка
`overdue_message_id` уже BigInteger (правильно), `dept_message_id` и
`arch_message_id` — Integer (time-bomb). ALTER COLUMN TYPE на маленьком
объёме данных мгновенен; новый тип совместим со старым диапазоном.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "l0m1n2o3p4q5"
down_revision: Union[str, None] = "k9l0m1n2o3p4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "tasks",
        "dept_message_id",
        existing_type=sa.Integer(),
        type_=sa.BigInteger(),
        existing_nullable=True,
    )
    op.alter_column(
        "tasks",
        "arch_message_id",
        existing_type=sa.Integer(),
        type_=sa.BigInteger(),
        existing_nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "tasks",
        "arch_message_id",
        existing_type=sa.BigInteger(),
        type_=sa.Integer(),
        existing_nullable=True,
    )
    op.alter_column(
        "tasks",
        "dept_message_id",
        existing_type=sa.BigInteger(),
        type_=sa.Integer(),
        existing_nullable=True,
    )
