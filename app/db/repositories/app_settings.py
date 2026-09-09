"""
Репозиторий глобальных настроек (app_settings).

Используется для значений, которые меняются админом через бота
(`TASK_CHAT_ID`, `LEADERSHIP_TOPIC_ID`), и должны переживать рестарт.

Контракт: методы НЕ коммитят сессию.
"""

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import AppSetting

KEY_TASK_CHAT_ID = "task_chat_id"
KEY_LEADERSHIP_TOPIC_ID = "leadership_topic_id"
KEY_OVERDUE_TOPIC_ID = "overdue_topic_id"
KEY_ONBOARDING_APPROVER_TG_ID = "onboarding_approver_tg_id"

# Env-фолбэки для ключей, без которых бот деградирует. Если кто-то очистит
# таблицу app_settings, эти значения подтянутся из .env.
_ENV_FALLBACKS: dict[str, int] = {
    KEY_TASK_CHAT_ID: settings.TASK_CHAT_ID,
    KEY_LEADERSHIP_TOPIC_ID: settings.LEADERSHIP_TOPIC_ID,
}


class AppSettingsRepository:
    @staticmethod
    async def get(session: AsyncSession, key: str) -> str | None:
        result = await session.execute(select(AppSetting.value).where(AppSetting.key == key))
        return result.scalar_one_or_none()

    @staticmethod
    async def get_int(session: AsyncSession, key: str) -> int | None:
        raw = await AppSettingsRepository.get(session, key)
        if raw is None or raw == "":
            # Если ключ не задан в БД — пробуем env-фолбэк (только для тех,
            # без которых бот деградирует). 0 в env означает «не задан».
            fallback = _ENV_FALLBACKS.get(key)
            if fallback is not None and fallback != 0:
                return fallback
            return None
        try:
            return int(raw)
        except ValueError:
            return None

    @staticmethod
    async def set(session: AsyncSession, key: str, value: str) -> None:
        stmt = insert(AppSetting).values(key=key, value=value)
        stmt = stmt.on_conflict_do_update(
            index_elements=[AppSetting.key],
            set_={"value": stmt.excluded.value},
        )
        await session.execute(stmt)
