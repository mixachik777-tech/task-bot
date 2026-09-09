"""task_history.event_type += question_asked, question_answered (Этап Г)

Revision ID: j8k9l0m1n2o3
Revises: i7j8k9l0m1n2
Create Date: 2026-05-27 17:40:00.000000+00:00

Этап Г «Уточняющие вопросы». Исполнитель может задать вопрос
постановщику прямо из карточки задачи; постановщик отвечает inline.
Оба события логируются в task_history. CHECK constraint
ck_task_history_event_type был узким (10 значений), при INSERT с
'question_asked' падало IntegrityError → callback бота висел на
прогрузке без ответа.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "j8k9l0m1n2o3"
down_revision: Union[str, None] = "i7j8k9l0m1n2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint("ck_task_history_event_type", "task_history", type_="check")
    op.create_check_constraint(
        "ck_task_history_event_type",
        "task_history",
        "event_type IN ('created','accepted','submitted_for_approval',"
        "'approved','rework_requested','completed','cancelled',"
        "'commented','reassigned','deadline_changed',"
        "'question_asked','question_answered')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_task_history_event_type", "task_history", type_="check")
    op.create_check_constraint(
        "ck_task_history_event_type",
        "task_history",
        "event_type IN ('created','accepted','submitted_for_approval',"
        "'approved','rework_requested','completed','cancelled',"
        "'commented','reassigned','deadline_changed')",
    )
