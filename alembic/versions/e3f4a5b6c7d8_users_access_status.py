"""users access_status + notified_admins_at

Revision ID: e3f4a5b6c7d8
Revises: d2e3f4a5b6c7
Create Date: 2026-05-18 18:00:00.000000+00:00

Добавляет в users поле access_status (pending|approved|denied) и
notified_admins_at — для кнопочного onboarding-флоу.

Бэкфилл: существующие is_active=TRUE → approved. is_active=FALSE → pending
(default). notified_admins_at не проставляется — при следующем /start
от ожидающих юзеров уйдёт уведомление аппруверу.

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e3f4a5b6c7d8"
down_revision: Union[str, None] = "d2e3f4a5b6c7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "access_status",
            sa.String(length=16),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
    )
    op.add_column(
        "users",
        sa.Column(
            "notified_admins_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.create_check_constraint(
        "ck_users_access_status",
        "users",
        "access_status IN ('pending','approved','denied')",
    )
    op.execute(
        "UPDATE users SET access_status='approved' WHERE is_active = TRUE"
    )


def downgrade() -> None:
    op.drop_constraint("ck_users_access_status", "users", type_="check")
    op.drop_column("users", "notified_admins_at")
    op.drop_column("users", "access_status")
