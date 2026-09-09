"""idx on tasks(creator_id)

Revision ID: a5b6c7d8e9f0
Revises: f4a5b6c7d8e9
Create Date: 2026-05-18 22:55:00.000000+00:00

Аналитика и AI-инструменты часто фильтруют задачи по постановщику
(scope='user', `get_user_tasks`). Без индекса — full table scan на каждом
запросе. Объём данных пока мал, но включаем индекс заранее.

"""
from typing import Sequence, Union

from alembic import op


revision: str = "a5b6c7d8e9f0"
down_revision: Union[str, None] = "f4a5b6c7d8e9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index("idx_tasks_creator", "tasks", ["creator_id"])


def downgrade() -> None:
    op.drop_index("idx_tasks_creator", table_name="tasks")
