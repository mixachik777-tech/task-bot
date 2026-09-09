"""
AuthMiddleware — точка входа в БД для авторизации.

Контракт:
- Приватный чат (chat.type == 'private'): get-or-create. Если юзера нет —
  создаёт запись с is_active=False, role='employee'.
- Не-приватный чат (group/supergroup): только lookup. Если юзер найден,
  кладёт его в data["user"]; если нет — пропускает дальше без user.
  Этим закрывается требование `/set_chat`/`/set_topic`/etc. — они
  выполняются в супергруппе и используют data["user"] из middleware.
- Кладёт ORM-объект `User` в data["user"]. expire_on_commit=False
  в async_session_factory → атрибуты доступны после commit'а.
"""

from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Chat, Message, TelegramObject
from loguru import logger

from app.db.base import async_session_factory
from app.db.repositories.users import UsersRepository


class AuthMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        chat = self._extract_chat(event)
        tg_user = getattr(event, "from_user", None)
        if chat is None or tg_user is None or tg_user.is_bot:
            return await handler(event, data)

        try:
            if chat.type == "private":
                async with async_session_factory() as session:
                    async with session.begin():
                        user = await UsersRepository.get_by_tg_id(session, tg_user.id)
                        if user is None:
                            user = await UsersRepository.create(
                                session,
                                tg_user_id=tg_user.id,
                                full_name=tg_user.full_name or f"User {tg_user.id}",
                                tg_username=tg_user.username,
                                is_active=False,
                            )
                data["user"] = user
            else:
                async with async_session_factory() as session:
                    async with session.begin():
                        user = await UsersRepository.get_by_tg_id(session, tg_user.id)
                if user is not None:
                    data["user"] = user
        except Exception:
            logger.exception(
                "AuthMiddleware failed for tg_user_id={} chat_type={}",
                tg_user.id,
                chat.type,
            )

        return await handler(event, data)

    @staticmethod
    def _extract_chat(event: TelegramObject) -> Chat | None:
        if isinstance(event, Message):
            return event.chat
        if isinstance(event, CallbackQuery):
            return event.message.chat if event.message else None
        return None
