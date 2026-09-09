"""
Callback-флоу одобрения новых пользователей.

Триггер — нажатие inline-кнопок в личке аппрувера (см.
services/onboarding_service.notify_approver). Все callback_data начинаются
с префикса `ob:`.

Шаги:
  ob:ap:{uid}              — Одобрить → показать выбор роли
  ob:rl:{uid}:{e|l|a}      — выбрана роль:
                              · a (admin) → активировать сразу
                              · e/l       → показать выбор отдела
  ob:dp:{uid}:{role}:{dc}  — выбран отдел → активация
  ob:dn:{uid}              — Отклонить
  ob:bk:{uid}              — Назад из выбора роли к карточке запроса
  ob:rq:{uid}              — повторная подача заявки от отклонённого юзера
                              (кнопка в /start у DENIED): сбрасываем
                              access_status в pending и шлём аппруверу
                              новое уведомление

Кнопки ob:ap/rl/dp/dn/bk активны только для tg_user_id, заданного в
app_settings.KEY_ONBOARDING_APPROVER_TG_ID — все остальные получают alert
«Только аппрувер может». ob:rq — от самого отклонённого юзера, поэтому
проверяется совпадение target.tg_user_id == cq.from_user.id. Защита от
двойного клика — условные апдейты в UsersRepository.approve/deny/
reset_to_pending (WHERE access_status=<ожидаемое>).
"""

from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import CallbackQuery
from loguru import logger

from app.bot.keyboards.main_menu import build_main_menu
from app.db.base import async_session_factory
from app.db.enums import AccessStatus, UserRole
from app.db.repositories.departments import DepartmentsRepository
from app.db.repositories.users import UsersRepository
from app.services.onboarding_service import (
    ROLE_RU,
    ROLE_SHORT,
    build_dept_keyboard,
    build_request_keyboard,
    build_role_keyboard,
    get_approver_tg_id,
    notify_approver,
    render_request_text,
)

router = Router(name="onboarding")


def _already_handled_alert(status: str) -> str:
    """Человеческий alert вместо «Уже обработан: APPROVED.» для аппрувера-2."""
    mapping = {
        "approved": "Эту заявку уже одобрил другой админ.",
        "denied": "Эту заявку уже отклонил другой админ.",
    }
    return mapping.get(status, "Эту заявку уже обработал другой админ.")


async def _check_is_approver(cq: CallbackQuery) -> bool:
    async with async_session_factory() as session:
        approver_tg_id = await get_approver_tg_id(session)
    if approver_tg_id is None:
        logger.error(
            "onboarding callback: approver_tg_id не задан в app_settings — "
            "никто не может одобрить запросы. Установи через SQL: "
            "UPDATE app_settings SET value='<tg_id>' WHERE key='onboarding_approver_tg_id'"
        )
        await cq.answer(
            "Аппрувер не настроен. Сообщите администратору бота.",
            show_alert=True,
        )
        return False
    if cq.from_user.id != approver_tg_id:
        await cq.answer(
            "Только назначенный аппрувер может обрабатывать запросы.",
            show_alert=True,
        )
        return False
    return True


def _parse_uid(data: str, prefix: str) -> int | None:
    """data='ob:ap:42', prefix='ob:ap:' → 42. None при ошибке."""
    try:
        rest = data.removeprefix(prefix)
        return int(rest.split(":", 1)[0])
    except (ValueError, AttributeError):
        return None


@router.callback_query(F.data.startswith("ob:ap:"))
async def cb_approve_start(cq: CallbackQuery) -> None:
    if not await _check_is_approver(cq):
        return
    uid = _parse_uid(cq.data or "", "ob:ap:")
    if uid is None or cq.message is None:
        await cq.answer()
        return

    async with async_session_factory() as session:
        target = await UsersRepository.get_by_id(session, uid)

    if target is None:
        await cq.answer("Пользователь не найден.", show_alert=True)
        return
    if target.access_status != AccessStatus.PENDING.value:
        await cq.answer(_already_handled_alert(target.access_status), show_alert=True)
        return

    try:
        await cq.message.edit_text(
            render_request_text(target) + "\n\nВыберите роль:",
            reply_markup=build_role_keyboard(uid),
        )
    except TelegramBadRequest:
        pass
    await cq.answer()


@router.callback_query(F.data.startswith("ob:bk:"))
async def cb_back_to_request(cq: CallbackQuery) -> None:
    if not await _check_is_approver(cq):
        return
    uid = _parse_uid(cq.data or "", "ob:bk:")
    if uid is None or cq.message is None:
        await cq.answer()
        return
    async with async_session_factory() as session:
        target = await UsersRepository.get_by_id(session, uid)
    if target is None:
        await cq.answer("Пользователь не найден.", show_alert=True)
        return
    if target.access_status != AccessStatus.PENDING.value:
        await cq.answer(_already_handled_alert(target.access_status), show_alert=True)
        return
    try:
        await cq.message.edit_text(
            render_request_text(target),
            reply_markup=build_request_keyboard(uid),
        )
    except TelegramBadRequest:
        pass
    await cq.answer()


@router.callback_query(F.data.startswith("ob:rl:"))
async def cb_role_chosen(cq: CallbackQuery, bot: Bot) -> None:
    if not await _check_is_approver(cq):
        return
    if cq.message is None or cq.data is None:
        await cq.answer()
        return
    rest = cq.data.removeprefix("ob:rl:")
    parts = rest.split(":")
    if len(parts) != 2:
        await cq.answer()
        return
    try:
        uid = int(parts[0])
    except ValueError:
        await cq.answer()
        return
    role_short = parts[1]
    role = ROLE_SHORT.get(role_short)
    if role is None:
        await cq.answer()
        return

    if role == UserRole.ADMIN:
        # Админ — без отдела, активируем сразу.
        await _finalize_approve(cq, bot, uid, role, dept_code=None)
        return

    # employee / lead — спросить отдел
    async with async_session_factory() as session:
        target = await UsersRepository.get_by_id(session, uid)
        depts = await DepartmentsRepository.list_active(session)

    if target is None:
        await cq.answer("Пользователь не найден.", show_alert=True)
        return
    if target.access_status != AccessStatus.PENDING.value:
        await cq.answer(_already_handled_alert(target.access_status), show_alert=True)
        return

    dept_pairs = [(d.code, d.name) for d in depts]
    try:
        await cq.message.edit_text(
            render_request_text(target) + f"\n\nРоль: <b>{ROLE_RU[role]}</b>\nВыберите отдел:",
            reply_markup=build_dept_keyboard(uid, role_short, dept_pairs),
        )
    except TelegramBadRequest:
        pass
    await cq.answer()


@router.callback_query(F.data.startswith("ob:dp:"))
async def cb_dept_chosen(cq: CallbackQuery, bot: Bot) -> None:
    if not await _check_is_approver(cq):
        return
    if cq.message is None or cq.data is None:
        await cq.answer()
        return
    rest = cq.data.removeprefix("ob:dp:")
    parts = rest.split(":", 2)
    if len(parts) != 3:
        await cq.answer()
        return
    try:
        uid = int(parts[0])
    except ValueError:
        await cq.answer()
        return
    role = ROLE_SHORT.get(parts[1])
    dept_code = parts[2]
    if role is None:
        await cq.answer()
        return
    await _finalize_approve(cq, bot, uid, role, dept_code=dept_code)


@router.callback_query(F.data.startswith("ob:dn:"))
async def cb_deny(cq: CallbackQuery, bot: Bot) -> None:
    if not await _check_is_approver(cq):
        return
    uid = _parse_uid(cq.data or "", "ob:dn:")
    if uid is None or cq.message is None:
        await cq.answer()
        return

    async with async_session_factory() as session:
        async with session.begin():
            target = await UsersRepository.get_by_id(session, uid)
            if target is None:
                await cq.answer("Пользователь не найден.", show_alert=True)
                return
            if target.access_status != AccessStatus.PENDING.value:
                await cq.answer(
                    _already_handled_alert(target.access_status),
                    show_alert=True,
                )
                return
            ok = await UsersRepository.deny(session, uid)
        # Перечитываем после коммита, чтобы рендерить актуальное состояние.
        target = await UsersRepository.get_by_id(session, uid)

    if not ok or target is None:
        await cq.answer("Не получилось обработать.", show_alert=True)
        return

    try:
        await cq.message.edit_text(
            render_request_text(target)
            + f"\n\n✖️ <b>Отклонён</b> @{cq.from_user.username or cq.from_user.id}",
            reply_markup=None,
        )
    except TelegramBadRequest:
        pass

    try:
        await bot.send_message(
            chat_id=target.tg_user_id,
            text=(
                "❌ Ваш запрос на доступ к боту отклонён.\n"
                "Если это ошибка — свяжитесь с менеджером."
            ),
        )
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        logger.warning(
            "onboarding deny: не смог уведомить tg_id={}: {}",
            target.tg_user_id,
            exc,
        )
    await cq.answer("Отклонено")


@router.callback_query(F.data.startswith("ob:rq:"))
async def cb_reapply(cq: CallbackQuery, bot: Bot) -> None:
    """Повторная подача заявки от отклонённого юзера.

    Эта кнопка отправляется самим юзером (из /start у DENIED), не аппрувером.
    Проверяем, что нажимает именно тот, кому принадлежит запись, и сбрасываем
    его access_status в pending — после чего шлём аппруверу новое уведомление.
    """
    uid = _parse_uid(cq.data or "", "ob:rq:")
    if uid is None or cq.message is None:
        await cq.answer()
        return

    async with async_session_factory() as session:
        async with session.begin():
            target = await UsersRepository.get_by_id(session, uid)
            if target is None:
                await cq.answer("Пользователь не найден.", show_alert=True)
                return
            if target.tg_user_id != cq.from_user.id:
                await cq.answer(
                    "Эта кнопка только для пользователя, которому был отказ в доступе.",
                    show_alert=True,
                )
                return
            if target.access_status == AccessStatus.PENDING.value:
                await cq.answer(
                    "Заявка уже отправлена менеджеру, ждите ответа.",
                    show_alert=True,
                )
                return
            if target.access_status == AccessStatus.APPROVED.value:
                await cq.answer(
                    "Доступ уже открыт. Меню в боте.",
                    show_alert=True,
                )
                return
            ok = await UsersRepository.reset_to_pending(session, uid)
            if not ok:
                await cq.answer(
                    "Не получилось обработать. Попробуйте ещё раз.",
                    show_alert=True,
                )
                return
            target = await UsersRepository.get_by_id(session, uid)
            if target is None:
                await cq.answer("Пользователь не найден.", show_alert=True)
                return
            sent = await notify_approver(bot, session, target)
            if sent:
                await UsersRepository.mark_notified(session, target.id)

    try:
        await cq.message.edit_text(
            "⏳ Заявка отправлена менеджеру повторно. Я напишу, когда доступ откроют.",
            reply_markup=None,
        )
    except TelegramBadRequest:
        pass
    await cq.answer("Заявка отправлена")


async def _finalize_approve(
    cq: CallbackQuery,
    bot: Bot,
    user_id: int,
    role: UserRole,
    dept_code: str | None,
) -> None:
    """Общая финализация одобрения: транзакция → уведомление юзера → UI."""
    if cq.message is None:
        await cq.answer()
        return

    async with async_session_factory() as session:
        async with session.begin():
            target = await UsersRepository.get_by_id(session, user_id)
            if target is None:
                await cq.answer("Пользователь не найден.", show_alert=True)
                return
            if target.access_status != AccessStatus.PENDING.value:
                await cq.answer(
                    _already_handled_alert(target.access_status),
                    show_alert=True,
                )
                return

            dept_id: int | None = None
            dept_name: str | None = None
            if dept_code is not None:
                dept = await DepartmentsRepository.get_by_code(session, dept_code)
                if dept is None:
                    await cq.answer(f"Отдел «{dept_code}» не найден.", show_alert=True)
                    return
                dept_id = dept.id
                dept_name = dept.name

            ok = await UsersRepository.approve(session, user_id, role, dept_id)
        target = await UsersRepository.get_by_id(session, user_id)

    if not ok or target is None:
        await cq.answer("Не получилось обработать.", show_alert=True)
        return

    role_ru = ROLE_RU[role]
    dept_suffix = f" в отделе «{dept_name}»" if dept_name else ""

    try:
        await cq.message.edit_text(
            render_request_text(target) + f"\n\n✅ <b>Одобрен</b> как {role_ru}{dept_suffix}",
            reply_markup=None,
        )
    except TelegramBadRequest:
        pass

    try:
        await bot.send_message(
            chat_id=target.tg_user_id,
            text="✅ Ваш аккаунт активирован. Меню открыто ниже.",
            reply_markup=build_main_menu(role),
        )
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        logger.warning(
            "onboarding approve: не смог уведомить tg_id={}: {}",
            target.tg_user_id,
            exc,
        )
    await cq.answer("Одобрено")
