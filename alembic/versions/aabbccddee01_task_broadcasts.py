"""task_broadcasts + outbox action='broadcast' (Этап А: рассылка в личку)

Revision ID: aabbccddee01
Revises: f4a5b6c7d8e9
Create Date: 2026-05-25 23:35:00.000000+00:00

Этап А «Бот без чата». Постановка задачи перестаёт публиковать карточку
в топик супергруппы и начинает рассылать её в личку каждому активному
участнику направления. Чтобы при принятии задачи убрать карточки у
остальных получателей, нужно знать какому юзеру какое сообщение мы
отправили — для этого таблица task_broadcasts.

Старые задачи в топиках доживают по прежней логике; миграция не
трогает уже опубликованные карточки.

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "aabbccddee01"
down_revision: Union[str, None] = "b6c7d8e9f0a1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "task_broadcasts",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "task_id",
            sa.BigInteger(),
            sa.ForeignKey("tasks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        # tg_user_id дублируется (есть в users.tg_user_id) — но при удалении
        # карточки у получателей нам нужен именно chat_id для bot.delete_message,
        # и платить JOIN на каждый клик «Принять» не хочется. Денормализация
        # ради скорости.
        sa.Column("tg_user_id", sa.BigInteger(), nullable=False),
        sa.Column("message_id", sa.Integer(), nullable=False),
        sa.Column(
            "sent_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("NOW()"),
            nullable=False,
        ),
        # Идемпотентность: повторный broadcast-outbox-attempt для той же
        # задачи не должен слать дубль конкретному юзеру.
        sa.UniqueConstraint("task_id", "user_id", name="uq_task_broadcasts_task_user"),
    )
    op.create_index("idx_task_broadcasts_task", "task_broadcasts", ["task_id"])

    # Расширяем перечень действий outbox: 'broadcast' (одна запись на задачу,
    # внутри обработчик идёт циклом по получателям из department).
    # DROP + ADD — CHECK constraint в Postgres не редактируется in-place.
    op.drop_constraint("ck_outbox_action", "task_outbox", type_="check")
    op.create_check_constraint(
        "ck_outbox_action",
        "task_outbox",
        "action IN ('post_dept','post_lead','broadcast')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_outbox_action", "task_outbox", type_="check")
    op.create_check_constraint(
        "ck_outbox_action",
        "task_outbox",
        "action IN ('post_dept','post_lead')",
    )
    op.drop_index("idx_task_broadcasts_task", table_name="task_broadcasts")
    op.drop_table("task_broadcasts")
