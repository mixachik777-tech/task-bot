"""tasks.status += awaiting_approval, task_history.event_type +=3 (Этап Б)

Revision ID: bbccddee0102
Revises: aabbccddee01
Create Date: 2026-05-25 23:55:00.000000+00:00

Этап Б «Согласование постановщиком». Между in_progress и done появляется
промежуточный статус awaiting_approval: исполнитель отправил результат,
ждём решения creator'а — «Согласовано» или «На доработку».

Расширяем CHECK constraints на:
- tasks.status: +'awaiting_approval'
- task_history.event_type: +'submitted_for_approval', +'approved', +'rework_requested'

"""

from typing import Sequence, Union

from alembic import op


revision: str = "bbccddee0102"
down_revision: Union[str, None] = "aabbccddee01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint("ck_tasks_status", "tasks", type_="check")
    op.create_check_constraint(
        "ck_tasks_status",
        "tasks",
        "status IN ('new','in_progress','awaiting_approval','done','cancelled')",
    )

    op.drop_constraint("ck_task_history_event_type", "task_history", type_="check")
    op.create_check_constraint(
        "ck_task_history_event_type",
        "task_history",
        "event_type IN ('created','accepted','submitted_for_approval',"
        "'approved','rework_requested','completed','cancelled',"
        "'commented','reassigned','deadline_changed')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_task_history_event_type", "task_history", type_="check")
    op.create_check_constraint(
        "ck_task_history_event_type",
        "task_history",
        "event_type IN ('created','accepted','completed','cancelled',"
        "'commented','reassigned','deadline_changed')",
    )

    op.drop_constraint("ck_tasks_status", "tasks", type_="check")
    op.create_check_constraint(
        "ck_tasks_status",
        "tasks",
        "status IN ('new','in_progress','done','cancelled')",
    )
