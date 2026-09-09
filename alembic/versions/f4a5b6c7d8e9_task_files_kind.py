"""task_files.kind (photo|document|video|animation)

Revision ID: f4a5b6c7d8e9
Revises: e3f4a5b6c7d8
Create Date: 2026-05-18 18:35:00.000000+00:00

Telegram отдаёт file_id, привязанный к типу вложения (Photo/Document/Video/
Animation). Эти пространства несовместимы — нельзя слать photo_file_id
через send_document. До этой миграции код всегда звал send_document, и
фотографии отваливались с 'can't use file of type Photo as Document'.

Добавляем явное поле kind. Бэкфилл для существующих:
  mime LIKE 'video/%'             → video
  mime = 'image/gif'              → animation
  mime LIKE 'image/%' AND file_name IS NULL → photo
  всё остальное                   → document

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f4a5b6c7d8e9"
down_revision: Union[str, None] = "e3f4a5b6c7d8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "task_files",
        sa.Column(
            "kind",
            sa.String(length=16),
            server_default=sa.text("'document'"),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "ck_task_files_kind",
        "task_files",
        "kind IN ('photo','document','video','animation')",
    )
    # Бэкфилл по эвристике mime+file_name.
    op.execute(
        """
        UPDATE task_files
        SET kind = CASE
            WHEN mime_type LIKE 'video/%' THEN 'video'
            WHEN mime_type = 'image/gif' THEN 'animation'
            WHEN mime_type LIKE 'image/%' AND file_name IS NULL THEN 'photo'
            ELSE 'document'
        END
        """
    )


def downgrade() -> None:
    op.drop_constraint("ck_task_files_kind", "task_files", type_="check")
    op.drop_column("task_files", "kind")
