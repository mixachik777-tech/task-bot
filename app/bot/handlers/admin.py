"""
Админ-команды супергруппы для настройки бота.

/set_chat                                  — сохранить chat.id текущей супергруппы
/set_topic <dept_code>                     — сохранить thread_id текущего топика
                                              как departments.topic_id
/set_leadership_topic                      — сохранить thread_id текущего топика
                                              как LEADERSHIP_TOPIC_ID

Активация юзеров — только через onboarding-флоу (inline-кнопки в личке
аппрувера, см. handlers/onboarding.py).
"""

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message

from app.bot.middlewares.role import require_role
from app.db.base import async_session_factory
from app.db.enums import UserRole
from app.db.models import User
from app.db.repositories.app_settings import (
    KEY_LEADERSHIP_TOPIC_ID,
    KEY_OVERDUE_TOPIC_ID,
    KEY_TASK_CHAT_ID,
    AppSettingsRepository,
)
from app.db.repositories.departments import DepartmentsRepository

router = Router(name="admin")

CB_LEAD_LIST = "adm:leads"


def _is_admin_user(user: User | None) -> bool:
    return user is not None and user.role == UserRole.ADMIN.value and user.is_active


async def _render_lead_list(bot: Bot) -> str:
    async with async_session_factory() as session:
        async with session.begin():
            task_chat_id = await AppSettingsRepository.get_int(session, KEY_TASK_CHAT_ID)
    if not task_chat_id:
        return "TASK_CHAT_ID ещё не настроен — нечего показать."
    try:
        admins = await bot.get_chat_administrators(chat_id=task_chat_id)
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        return f"Не удалось получить список админов: {exc}"
    lines = ["Кто может писать в закрытую тему «Руководство»:"]
    for member in admins:
        u = member.user
        status_marker = {"creator": "👑", "administrator": "🛡"}.get(member.status, "•")
        handle = f"@{u.username}" if u.username else u.full_name
        can_manage = getattr(member, "can_manage_topics", None)
        can_marker = " (manage topics)" if can_manage else ""
        lines.append(f"{status_marker} {handle} <code>{u.id}</code>{can_marker}")
    lines.append(
        "\nЧтобы добавить кого-то: открой настройки супергруппы → "
        "«Администраторы» → «Добавить администратора» → выбери человека "
        "→ оставь только право «Управление темами»."
    )
    return "\n".join(lines)


@router.callback_query(F.data == CB_LEAD_LIST)
async def cb_lead_list(cq: CallbackQuery, bot: Bot, user: User | None = None) -> None:
    """Кнопка «🛡 Лиды чата» из Кабинета админа — показывает админов
    супергруппы, у которых есть право писать в тему «Руководство»."""
    if not _is_admin_user(user):
        await cq.answer("Раздел только для админа.", show_alert=True)
        return
    text = await _render_lead_list(bot)
    if cq.message is not None:
        await cq.message.answer(text)
    await cq.answer()


@router.message(Command("set_chat"), require_role(UserRole.ADMIN))
async def cmd_set_chat(message: Message) -> None:
    if message.chat.type not in {"supergroup", "group"}:
        await message.answer("⚠️ Эта команда выполняется в супергруппе редакции, не в личке.")
        return
    chat_id = message.chat.id
    async with async_session_factory() as session:
        async with session.begin():
            await AppSettingsRepository.set(session, KEY_TASK_CHAT_ID, str(chat_id))
    await message.answer(f"✅ TASK_CHAT_ID сохранён: <code>{chat_id}</code>")


@router.message(Command("set_topic"), require_role(UserRole.ADMIN))
async def cmd_set_topic(message: Message, command: CommandObject) -> None:
    if message.chat.type not in {"supergroup", "group"}:
        await message.answer("⚠️ Эта команда выполняется в супергруппе редакции.")
        return
    if not message.is_topic_message or message.message_thread_id is None:
        await message.answer("⚠️ Эта команда выполняется внутри топика отдела.")
        return
    if not command.args:
        await message.answer("Использование: /set_topic &lt;dept_code&gt;")
        return
    dept_code = command.args.strip()
    topic_id = message.message_thread_id
    async with async_session_factory() as session:
        async with session.begin():
            dept = await DepartmentsRepository.get_by_code(session, dept_code)
            if dept is None:
                await message.answer(f"Отдел «{dept_code}» не найден.")
                return
            await DepartmentsRepository.update_topic_id(session, dept.id, topic_id)
    await message.answer(f"✅ topic_id для отдела «{dept_code}» сохранён: <code>{topic_id}</code>")


@router.message(Command("set_leadership_topic"), require_role(UserRole.ADMIN))
async def cmd_set_leadership_topic(message: Message) -> None:
    if message.chat.type not in {"supergroup", "group"}:
        await message.answer("⚠️ Эта команда выполняется в супергруппе редакции.")
        return
    if not message.is_topic_message or message.message_thread_id is None:
        await message.answer("⚠️ Эта команда выполняется внутри топика «Руководство».")
        return
    topic_id = message.message_thread_id
    async with async_session_factory() as session:
        async with session.begin():
            await AppSettingsRepository.set(session, KEY_LEADERSHIP_TOPIC_ID, str(topic_id))
    await message.answer(f"✅ LEADERSHIP_TOPIC_ID сохранён: <code>{topic_id}</code>")


@router.message(Command("set_overdue_topic"), require_role(UserRole.ADMIN))
async def cmd_set_overdue_topic(message: Message) -> None:
    """Команда выполняется внутри созданного админом топика «Просроченные».

    Сохраняет topic_id в app_settings под ключом overdue_topic_id. После
    этого scheduler.check_overdue будет публиковать сюда карточку любой
    задачи, перешедшей в просрочку, а покидание просрочки (approve/cancel)
    удалит карточку из топика.
    """
    if message.chat.type not in {"supergroup", "group"}:
        await message.answer("⚠️ Эта команда выполняется в супергруппе редакции.")
        return
    if not message.is_topic_message or message.message_thread_id is None:
        await message.answer("⚠️ Эта команда выполняется внутри топика «Просроченные».")
        return
    topic_id = message.message_thread_id
    async with async_session_factory() as session:
        async with session.begin():
            await AppSettingsRepository.set(session, KEY_OVERDUE_TOPIC_ID, str(topic_id))
    await message.answer(
        f"✅ OVERDUE_TOPIC_ID сохранён: <code>{topic_id}</code>\n"
        "Просроченные задачи будут публиковаться сюда автоматически "
        "(карточка появится при первом запуске check_overdue с просрочкой)."
    )
