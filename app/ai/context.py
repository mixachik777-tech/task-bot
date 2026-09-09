"""
Контекст вызова AI-агента.

UserContext — иммутабельный snapshot: кто спрашивает, его роль, отдел и
текущее время. Прокидывается во все инструменты, чтобы каждый сам мог
проверить permissions и подставить дефолтный user_id (для запросов
вида «мои задачи»).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from app.db.models import User


@dataclass(frozen=True)
class UserContext:
    user_id: int  # User.id, внутренний (не tg_user_id)
    role: str
    department_id: int | None
    full_name: str
    tg_username: str | None
    now_utc: datetime

    @classmethod
    def from_user(cls, user: User, *, now: datetime | None = None) -> "UserContext":
        return cls(
            user_id=user.id,
            role=user.role,
            department_id=user.department_id,
            full_name=user.full_name or f"id={user.tg_user_id}",
            tg_username=user.tg_username,
            now_utc=now or datetime.now(timezone.utc),
        )
