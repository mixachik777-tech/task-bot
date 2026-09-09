"""
Onboarding-флоу: уведомление аппрувера о новом запросе доступа +
сборка инлайн-клавиатур для трёхступенчатого approve (роль → отдел).

Аппрувер — один tg_user_id из app_settings.KEY_ONBOARDING_APPROVER_TG_ID.
Если ключ не задан / лички недоступны — нотификация не отправляется,
но это не блокирует /start юзера (он всё равно увидит «ожидайте»).
"""

from __future__ import annotations

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import UserRole
from app.db.models import User
from app.db.repositories.app_settings import (
    KEY_ONBOARDING_APPROVER_TG_ID,
    AppSettingsRepository,
)

# Короткие коды ролей в callback_data (экономим 64-байтовый лимит).
ROLE_SHORT: dict[str, UserRole] = {
    "e": UserRole.EMPLOYEE,
    "l": UserRole.LEAD,
    "a": UserRole.ADMIN,
}
ROLE_TO_SHORT: dict[UserRole, str] = {v: k for k, v in ROLE_SHORT.items()}
ROLE_RU: dict[UserRole, str] = {
    UserRole.EMPLOYEE: "Сотрудник",
    UserRole.LEAD: "Лид",
    UserRole.ADMIN: "Админ",
}


async def get_approver_tg_id(session: AsyncSession) -> int | None:
    return await AppSettingsRepository.get_int(session, KEY_ONBOARDING_APPROVER_TG_ID)


def build_request_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Одобрить", callback_data=f"ob:ap:{user_id}"),
                InlineKeyboardButton(text="✖️ Отклонить", callback_data=f"ob:dn:{user_id}"),
            ]
        ]
    )


def build_role_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Сотрудник", callback_data=f"ob:rl:{user_id}:e"),
                InlineKeyboardButton(text="Лид", callback_data=f"ob:rl:{user_id}:l"),
                InlineKeyboardButton(text="Админ", callback_data=f"ob:rl:{user_id}:a"),
            ],
            [
                InlineKeyboardButton(text="⬅️ Назад", callback_data=f"ob:bk:{user_id}"),
            ],
        ]
    )


def build_dept_keyboard(
    user_id: int, role_short: str, depts: list[tuple[str, str]]
) -> InlineKeyboardMarkup:
    """depts: [(code, name), …] активных отделов."""
    rows: list[list[InlineKeyboardButton]] = []
    for code, name in depts:
        rows.append(
            [
                InlineKeyboardButton(
                    text=name,
                    callback_data=f"ob:dp:{user_id}:{role_short}:{code}",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"ob:ap:{user_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def render_request_text(user: User) -> str:
    import html

    username = f"@{html.escape(user.tg_username)}" if user.tg_username else "без username"
    return (
        "🆕 <b>Запрос на доступ к боту</b>\n\n"
        f"Имя: {html.escape(user.full_name or '')}\n"
        f"Telegram: {username}\n"
        f"ID: <code>{user.tg_user_id}</code>"
    )


async def notify_approver(bot: Bot, session: AsyncSession, target: User) -> bool:
    """Шлёт аппруверу нотификацию о target. True — если успех.

    На вход — User в состоянии access_status='pending'. Сам апдейт
    notified_admins_at делает вызывающая сторона при успехе.
    """
    approver_tg_id = await get_approver_tg_id(session)
    if approver_tg_id is None:
        logger.warning("onboarding: approver_tg_id не задан в app_settings — нотификация пропущена")
        return False
    try:
        await bot.send_message(
            chat_id=approver_tg_id,
            text=render_request_text(target),
            reply_markup=build_request_keyboard(target.id),
        )
        return True
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        logger.warning(
            "onboarding: не смог уведомить approver tg_id={}: {}",
            approver_tg_id,
            exc,
        )
        return False
