"""leadership_whitelist

Revision ID: c1d2e3f4a5b6
Revises: b6f4a1e25c08
Create Date: 2026-05-14 00:50:00.000000+00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c1d2e3f4a5b6"
down_revision: Union[str, None] = "b6f4a1e25c08"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "leadership_whitelist",
        sa.Column("tg_user_id", sa.BigInteger(), nullable=False),
        sa.Column("note", sa.String(length=128), nullable=True),
        sa.Column("added_by", sa.BigInteger(), nullable=False),
        sa.Column(
            "added_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("NOW()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("tg_user_id"),
    )


def downgrade() -> None:
    op.drop_table("leadership_whitelist")
