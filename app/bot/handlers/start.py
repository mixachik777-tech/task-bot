"""Хэндлер /start. Решает, что показать: «отказано» / «ожидайте» / меню."""

from aiogram import Bot, Router
from aiogram.filters import CommandStart
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardRemove,
)
from loguru import logger

from app.bot.keyboards.main_menu import build_main_menu
from app.db.base import async_session_factory
from app.db.enums import AccessStatus, UserRole
from app.db.models import User
from app.db.repositories.users import UsersRepository
from app.services.onboarding_service import notify_approver

router = Router(name="start")


def _denied_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔄 Подать заявку повторно",
                    callback_data=f"ob:rq:{user_id}",
                )
            ]
        ]
    )


@router.message(CommandStart())
async def cmd_start(message: Message, user: User, bot: Bot) -> None:
    if user.access_status == AccessStatus.DENIED.value:
        await message.answer(
            "❌ Доступ к боту отклонён.\n"
            "Если это ошибка — нажмите «Подать заявку повторно», "
            "и менеджер увидит новый запрос.",
            reply_markup=_denied_keyboard(user.id),
        )
        return

    if user.access_status == AccessStatus.PENDING.value or not user.is_active:
        await message.answer(
            "⏳ Ваш аккаунт ожидает одобрения администратором. Я напишу, когда доступ откроют.",
            reply_markup=ReplyKeyboardRemove(),
        )
        # При первом /start уведомляем аппрувера и помечаем notified_admins_at,
        # чтобы повторные /start не дублировали уведомление.
        if user.notified_admins_at is None:
            try:
                async with async_session_factory() as session:
                    async with session.begin():
                        sent = await notify_approver(bot, session, user)
                        if sent:
                            await UsersRepository.mark_notified(session, user.id)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "start: ошибка уведомления аппрувера для user_id={}",
                    user.id,
                )
        return

    import html

    await message.answer(
        f"Здравствуйте, {html.escape(user.full_name or '')}!",
        reply_markup=build_main_menu(UserRole(user.role)),
    )
