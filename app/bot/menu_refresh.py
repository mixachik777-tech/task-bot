"""
MenuAwareBot — подкласс aiogram.Bot, который автоматически прицепляет свежую
reply-клавиатуру главного меню к send_message в приватных чатах, если у
получателя на стороне Telegram-клиента закеширована устаревшая версия.

Зачем: Telegram кеширует ReplyKeyboardMarkup per chat. При добавлении/удалении
кнопок старый кеш остаётся, пока бот не пришлёт сообщение с новым reply_markup.
Без этого механизма обновление меню доходит до пользователя только при /start
или другом редком пути, где явно вызывается build_main_menu.

Контракт:
- Срабатывает только для send_message в личку (chat_id > 0).
- Не трогает сообщения, где handler уже задал reply_markup (inline,
  ReplyKeyboardRemove, кастомный reply) — приоритет за handler'ом.
- Не трогает группы/каналы (chat_id < 0).
- Pre-check через Redis (menu:ver:<tg_id>) — O(1), DB-запрос только при
  расхождении версии (первый интеракт после деплоя).
- Pending/denied/удалённые пользователи получают сообщение без меню.
- Один промах в Redis/БД — log+skip, не роняет основной поток.
"""

from __future__ import annotations

from typing import Any

from aiogram import Bot
from loguru import logger
from redis.asyncio import Redis

from app.bot.keyboards.main_menu import MENU_VERSION, build_main_menu
from app.db.base import async_session_factory
from app.db.enums import AccessStatus, UserRole
from app.db.repositories.users import UsersRepository

MENU_VERSION_KEY_FMT = "menu:ver:{tg_user_id}"
MENU_VERSION_TTL_S = 60 * 60 * 24 * 365  # 1 год


class MenuAwareBot(Bot):
    """Bot с авто-инжектом свежего главного меню в send_message приватного чата."""

    def __init__(self, token: str, *, redis: Redis, **kwargs: Any) -> None:
        super().__init__(token=token, **kwargs)
        self._menu_redis = redis

    async def send_message(  # type: ignore[override]
        self,
        chat_id: int | str,
        text: str,
        **kwargs: Any,
    ) -> Any:
        if kwargs.get("reply_markup") is None and isinstance(chat_id, int) and chat_id > 0:
            markup = await self._maybe_get_menu_for(chat_id)
            if markup is not None:
                kwargs["reply_markup"] = markup
        return await super().send_message(chat_id=chat_id, text=text, **kwargs)

    async def _maybe_get_menu_for(self, tg_user_id: int):
        key = MENU_VERSION_KEY_FMT.format(tg_user_id=tg_user_id)

        try:
            current = await self._menu_redis.get(key)
        except Exception:
            logger.exception("menu_refresh: redis get failed for tg_id={}", tg_user_id)
            return None

        if current is not None:
            value = current.decode() if isinstance(current, bytes) else current
            if value == MENU_VERSION:
                return None

        try:
            async with async_session_factory() as session:
                user = await UsersRepository.get_by_tg_id(session, tg_user_id)
        except Exception:
            logger.exception("menu_refresh: db lookup failed for tg_id={}", tg_user_id)
            return None

        if user is None or not user.is_active:
            return None
        if user.access_status != AccessStatus.APPROVED.value:
            return None

        try:
            markup = build_main_menu(UserRole(user.role))
        except Exception:
            logger.exception(
                "menu_refresh: build_main_menu failed for tg_id={} role={}",
                tg_user_id,
                user.role,
            )
            return None

        try:
            await self._menu_redis.set(key, MENU_VERSION, ex=MENU_VERSION_TTL_S)
        except Exception:
            logger.exception(
                "menu_refresh: redis set failed for tg_id={} (sending menu anyway)",
                tg_user_id,
            )

        logger.info(
            "menu_refresh: injected version={} for tg_id={} role={}",
            MENU_VERSION,
            tg_user_id,
            user.role,
        )
        return markup
