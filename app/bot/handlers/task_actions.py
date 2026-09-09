"""
Callback-хэндлеры карточки задачи: «✋ Принять», «✅ Завершить»,
«❌ Отменить», «💬 Комментарий» + FSM ввода result_comment / comment-text.

Защита от двойного клика — внутри TaskActionsService через SELECT FOR UPDATE.
Permission-checks — здесь, до вызова сервиса (creator/assignee/admin/lead).
"""

import asyncio
import time
from typing import Any

from aiogram import Bot, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import default_state
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from loguru import logger

from app.bot.keyboards.main_menu import build_main_menu
from app.db.enums import UserRole
from app.bot.keyboards.task_card import (
    build_approval_kb,
    build_cancel_confirm_kb,
    build_complete_skip_kb,
    build_rework_finalize_kb,
    build_task_card_kb,
)
from app.bot.states import RequestRework, TaskComment, TaskComplete
from app.bot.utils.file_extract import collect_files_from_messages
from app.db.base import async_session_factory
from app.db.exceptions import InvalidStatusTransition
from app.db.models import User
from app.db.repositories.tasks import TasksRepository
from app.services.task_actions import TaskActionsService

router = Router(name="task_actions")


async def _restore_approval_kb_from_state(bot: Bot, state_data: dict) -> None:
    """Возвращает кнопки [✅ Согласовано / ✏️ На доработку] на исходное
    сообщение постановщика. Используется при отмене FSM RequestRework —
    иначе после _strip_approval_kb юзер теряет возможность согласовать
    или повторно отправить на доработку.

    Координаты сообщения и task_id берутся из state, который заполнил
    cb_rework_start.
    """
    chat_id = state_data.get("approval_chat_id")
    msg_id = state_data.get("approval_message_id")
    task_id = state_data.get("task_id")
    if chat_id is None or msg_id is None or task_id is None:
        return
    try:
        await bot.edit_message_reply_markup(
            chat_id=int(chat_id),
            message_id=int(msg_id),
            reply_markup=build_approval_kb(int(task_id)),
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("restore approval kb failed for task #{}: {}", task_id, exc)


# Максимальная пауза между «нажал кнопку и попросили текст» и реальным
# ответом. Если пройдёт больше — FSM сбрасывается, чтобы юзер не висел
# в waiting-state и следующий /start работал штатно.
FSM_WAIT_TTL_SECONDS = 10 * 60


async def _fsm_is_fresh(state: FSMContext) -> bool:
    """Проверяет, что FSM ещё «живой»: started_at в data моложе TTL.
    Если поля started_at нет (старая сессия до апдейта) — считаем свежим.

    При успехе обновляет started_at: TTL = таймер бездействия, не общего
    времени сессии. Иначе активный юзер, который шлёт сообщения каждую
    минуту (сокращает длинный текст по «сократи и повтори»), упрётся в
    expired через 10 мин от первого клика, FSM умрёт, бот замолчит.
    """
    data = await state.get_data()
    started_at = data.get("started_at")
    if started_at is None:
        await state.update_data(started_at=time.monotonic())
        return True
    try:
        if (time.monotonic() - float(started_at)) <= FSM_WAIT_TTL_SECONDS:
            await state.update_data(started_at=time.monotonic())
            return True
        return False
    except (TypeError, ValueError):
        await state.update_data(started_at=time.monotonic())
        return True


# ──────────────────────────────────────────────────────────────────────
# helpers
# ──────────────────────────────────────────────────────────────────────


def _parse_task_id(data: str | None) -> int | None:
    if not data or ":" not in data:
        return None
    try:
        return int(data.split(":", 1)[1])
    except ValueError:
        return None


async def _ensure_user(cq: CallbackQuery, user: User | None) -> User | None:
    """
    Проверяет, что user в data есть и активирован.
    is_active=False (свежий запуск, ещё не одобрен админом) — отказ:
    иначе случайный посторонний из лички мог бы принять/отменить задачу
    из супергруппы (он добавлен в users get-or-create в AuthMiddleware,
    но без одобрения через onboarding-флоу).
    """
    if user is None:
        await cq.answer(
            "Сначала откройте бота в личке и нажмите кнопку Start, чтобы зарегистрироваться.",
            show_alert=True,
        )
        return None
    if not user.is_active:
        await cq.answer(
            "⏳ Ваш аккаунт ещё не активирован. Дождитесь одобрения админа.",
            show_alert=True,
        )
        return None
    return user


# ──────────────────────────────────────────────────────────────────────
# task_accept
# ──────────────────────────────────────────────────────────────────────


@router.callback_query(F.data.startswith("task_accept:"))
async def cb_accept(cq: CallbackQuery, bot: Bot, user: User | None = None) -> None:
    user = await _ensure_user(cq, user)
    if user is None:
        return
    task_id = _parse_task_id(cq.data)
    if task_id is None:
        await cq.answer("Некорректная задача.", show_alert=True)
        return

    try:
        result = await TaskActionsService.accept(bot=bot, task_id=task_id, user=user)
    except InvalidStatusTransition:
        await cq.answer(
            "Действие уже невозможно — задача в другом статусе.",
            show_alert=True,
        )
        return
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — финальный edge перед UI
        logger.exception("task_accept failed for #{}: {}", task_id, exc)
        await cq.answer("Ошибка. Попробуй ещё раз позже.", show_alert=True)
        return

    await cq.answer(result.user_message, show_alert=not result.success)


# ──────────────────────────────────────────────────────────────────────
# task_cancel
# ──────────────────────────────────────────────────────────────────────


@router.callback_query(F.data.startswith("tcancel_ask:"))
async def cb_cancel_ask(cq: CallbackQuery, user: User | None = None) -> None:
    """Подтверждение снятия задачи. Меняет inline-kb текущего сообщения
    на confirm-клавиатуру [✅ Да, снять / ⬅️ Назад]. Сама отмена идёт
    в cb_cancel при тапе «✅ Да, снять» (callback task_cancel:{id})."""
    user = await _ensure_user(cq, user)
    if user is None:
        return
    task_id = _parse_task_id(cq.data)
    if task_id is None:
        await cq.answer("Некорректная задача.", show_alert=True)
        return
    if isinstance(cq.message, Message):
        try:
            await cq.message.edit_reply_markup(reply_markup=build_cancel_confirm_kb(task_id))
        except Exception as exc:  # noqa: BLE001
            logger.debug("tcancel_ask edit_reply_markup failed: {}", exc)
    await cq.answer(
        "Снять задачу? Это переведёт её в архив со статусом «Отменена».",
        show_alert=True,
    )


@router.callback_query(F.data.startswith("tcancel_back:"))
async def cb_cancel_back(cq: CallbackQuery, user: User | None = None) -> None:
    """«⬅️ Назад» из confirm-диалога — возвращаем исходный набор кнопок
    действий по текущему статусу задачи."""
    user = await _ensure_user(cq, user)
    if user is None:
        return
    task_id = _parse_task_id(cq.data)
    if task_id is None:
        await cq.answer("Некорректная задача.", show_alert=True)
        return
    async with async_session_factory() as session:
        async with session.begin():
            task = await TasksRepository.get_by_id(session, task_id)
    if task is None:
        await cq.answer("Задача не найдена.", show_alert=True)
        return
    kb = build_task_card_kb(task.status, task.id)
    if isinstance(cq.message, Message):
        try:
            await cq.message.edit_reply_markup(reply_markup=kb)
        except Exception as exc:  # noqa: BLE001
            logger.debug("tcancel_back edit_reply_markup failed: {}", exc)
    await cq.answer()


@router.callback_query(F.data.startswith("task_cancel:"))
async def cb_cancel(cq: CallbackQuery, bot: Bot, user: User | None = None) -> None:
    user = await _ensure_user(cq, user)
    if user is None:
        return
    task_id = _parse_task_id(cq.data)
    if task_id is None:
        await cq.answer("Некорректная задача.", show_alert=True)
        return

    try:
        result = await TaskActionsService.cancel(bot=bot, task_id=task_id, user=user)
    except InvalidStatusTransition:
        await cq.answer(
            "Действие уже невозможно — задача в другом статусе.",
            show_alert=True,
        )
        return
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("task_cancel failed for #{}: {}", task_id, exc)
        await cq.answer("Ошибка. Попробуй ещё раз позже.", show_alert=True)
        return

    # Если отмена успешна — снимаем inline-kb с текущего сообщения,
    # чтобы юзер не мог нажать кнопки на уже отменённой задаче.
    if result.success and isinstance(cq.message, Message):
        try:
            await cq.message.edit_reply_markup(reply_markup=None)
        except Exception as exc:  # noqa: BLE001
            logger.debug("cb_cancel strip kb failed: {}", exc)
    await cq.answer(result.user_message, show_alert=not result.success)


# ──────────────────────────────────────────────────────────────────────
# task_complete — накопитель отчёта (текст + файлы) и отправка постановщику
# ──────────────────────────────────────────────────────────────────────

MAX_RESULT_FILES = 10  # верхний предел — чтобы не зафлудить личку постановщика
MAX_RESULT_COMMENT_LEN = 4096


def _file_from_message(message: Message) -> dict[str, str | None] | None:
    """
    Извлекает file_id+kind+file_name из сообщения с вложением.

    Поддерживаем document/photo/video/animation — самые ходовые типы для
    редакционного контента (макет/готовый текст/афиша/рилз).
    Голосовые, стикеры, видео-кружки, опросы — игнорим: для отчёта о
    выполненной задаче это нерелевантный шум.
    """
    if message.document is not None:
        return {
            "kind": "document",
            "file_id": message.document.file_id,
            "file_name": message.document.file_name,
        }
    if message.photo:
        # photo — массив размеров, file_id последнего = оригинал максимального качества
        return {
            "kind": "photo",
            "file_id": message.photo[-1].file_id,
            "file_name": None,
        }
    if message.video is not None:
        return {
            "kind": "video",
            "file_id": message.video.file_id,
            "file_name": message.video.file_name,
        }
    if message.animation is not None:
        return {
            "kind": "animation",
            "file_id": message.animation.file_id,
            "file_name": message.animation.file_name,
        }
    return None


@router.callback_query(F.data.startswith("task_complete:"))
async def cb_complete_start(
    cq: CallbackQuery,
    state: FSMContext,
    user: User | None = None,
) -> None:
    user = await _ensure_user(cq, user)
    if user is None:
        return
    task_id = _parse_task_id(cq.data)
    if task_id is None:
        await cq.answer("Некорректная задача.", show_alert=True)
        return

    async with async_session_factory() as session:
        disp = await TasksRepository.get_display_number(session, task_id)
    disp = disp or task_id

    await state.set_state(TaskComplete.waiting_comment)
    await state.update_data(
        task_id=task_id,
        display_number=disp,
        started_at=time.monotonic(),
        comment=None,
        files=[],
    )
    if isinstance(cq.message, Message):
        await cq.message.answer(
            f"<b>Отправка отчёта по задаче #{disp}</b>\n\n"
            "<b>Шаг 1.</b> Напиши результат текстом — просто отправь сообщение "
            "в чат снизу.\n"
            "<b>Шаг 2.</b> Приложи готовый файл: нажми 📎 (скрепка) слева от "
            "поля ввода → «Файл» или «Фото/видео» → выбери файл → отправь. "
            "Можно отправить несколько файлов разными сообщениями — всё копится.\n"
            "<b>Шаг 3.</b> Нажми кнопку «✅ Готово, завершить» ниже. Задача "
            "уйдёт постановщику на согласование.\n\n"
            "Если отчёт не нужен — «Без отчёта». Если передумал — «❌ Отмена».\n"
            f"Лимиты: текст до {MAX_RESULT_COMMENT_LEN} символов, "
            f"вложений до {MAX_RESULT_FILES}.",
            reply_markup=build_complete_skip_kb(task_id),
        )
    await cq.answer()


@router.message(
    StateFilter(TaskComplete.waiting_comment),
    F.document | F.photo | F.video | F.animation,
)
async def msg_complete_collect_file(
    message: Message,
    state: FSMContext,
    user: User | None = None,
    album: list[Message] | None = None,
) -> None:
    """Накопитель файлов: атомарно по батчу (альбом → один write в FSM)."""
    if user is None or not user.is_active:
        await message.answer("⏳ Аккаунт не активирован, действие невозможно.")
        await state.clear()
        return
    if not await _fsm_is_fresh(state):
        await state.clear()
        await message.answer("Время ожидания истекло. Открой задачу и нажми «Завершить» ещё раз.")
        return

    incoming: list[Message] = album if album else [message]
    data = await state.get_data()
    files: list[dict[str, Any]] = list(data.get("files") or [])
    current_count = len(files)

    accepted, oversize, overflow = collect_files_from_messages(
        incoming,
        extractor=_file_from_message,
        current_count=current_count,
        max_count=MAX_RESULT_FILES,
    )
    if accepted:
        files.extend(accepted)
        await state.update_data(files=files)

    # Подпись (caption) берём ТОЛЬКО с первого сообщения альбома — Telegram
    # отдаёт caption только на одной из позиций (обычно первой); собирать
    # со всех бессмысленно (дубли). И в обычном single-сценарии caption
    # тоже один.
    caption = (incoming[0].caption or "").strip() if incoming else ""
    if caption:
        cur = (data.get("comment") or "").strip()
        merged = (cur + ("\n" if cur else "") + caption)[:MAX_RESULT_COMMENT_LEN]
        await state.update_data(comment=merged)

    notes: list[str] = []
    if oversize:
        notes.append(f"⚠️ {oversize} файл(ов) больше 20 МБ — не принял.")
    if overflow:
        notes.append(
            f"⚠️ {overflow} файл(ов) не вошли в лимит {MAX_RESULT_FILES}. Заверши с тем что есть."
        )

    fresh = await state.get_data()
    summary = _accumulator_summary(
        comment=fresh.get("comment"),
        files=list(fresh.get("files") or []),
    )
    task_id = data.get("task_id")
    kb = build_complete_skip_kb(int(task_id)) if task_id is not None else None
    body = "\n\n".join([f"📎 Принял. {summary}", *notes])
    await message.answer(body, reply_markup=kb)


@router.message(StateFilter(TaskComplete.waiting_comment), F.text & ~F.text.startswith("/"))
async def msg_complete_collect_text(
    message: Message,
    state: FSMContext,
    user: User | None = None,
) -> None:
    """Накопитель текста: каждое текстовое сообщение → перезаписывает comment."""
    if user is None or not user.is_active:
        await message.answer("⏳ Аккаунт не активирован, действие невозможно.")
        await state.clear()
        return
    if not await _fsm_is_fresh(state):
        await state.clear()
        await message.answer("Время ожидания истекло. Открой задачу и нажми «Завершить» ещё раз.")
        return

    text = (message.text or "").strip()
    if not text:
        return
    if len(text) > MAX_RESULT_COMMENT_LEN:
        await message.answer(
            f"Комментарий длиннее {MAX_RESULT_COMMENT_LEN} символов. Сократи и повтори."
        )
        return

    data = await state.get_data()
    await state.update_data(comment=text)
    summary = _accumulator_summary(comment=text, files=list(data.get("files") or []))
    task_id = data.get("task_id")
    kb = build_complete_skip_kb(int(task_id)) if task_id is not None else None
    await message.answer(f"💬 Принял. {summary}", reply_markup=kb)


def _accumulator_summary(*, comment: str | None, files: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    if comment:
        parts.append(f"комментарий ✓ ({len(comment)} симв.)")
    if files:
        parts.append(f"вложений ✓ ({len(files)})")
    if not parts:
        return "Пока пусто."
    return (
        "Сейчас в отчёте: "
        + ", ".join(parts)
        + ". Можно добавить ещё или нажать «✅ Готово, завершить»."
    )


@router.callback_query(
    StateFilter(TaskComplete.waiting_comment),
    F.data.startswith("tcomp_done:"),
)
async def cb_complete_done(
    cq: CallbackQuery,
    state: FSMContext,
    bot: Bot,
    user: User | None = None,
) -> None:
    """«✅ Готово, завершить» — собрать накопленное и завершить задачу."""
    user = await _ensure_user(cq, user)
    if user is None:
        return
    data = await state.get_data()
    task_id = data.get("task_id")
    comment = data.get("comment")
    files: list[dict[str, Any]] = list(data.get("files") or [])
    await state.clear()
    if task_id is None:
        await cq.answer("Состояние утеряно. Повтори.", show_alert=True)
        return

    await _do_complete(
        cq=cq,
        bot=bot,
        task_id=int(task_id),
        user=user,
        comment=comment,
        files=files,
    )


@router.callback_query(
    StateFilter(TaskComplete.waiting_comment),
    F.data.startswith("tcomp_skip:"),
)
async def cb_complete_skip(
    cq: CallbackQuery,
    state: FSMContext,
    bot: Bot,
    user: User | None = None,
) -> None:
    """«Без отчёта» — завершить сразу, ничего не отправлять (старое поведение)."""
    user = await _ensure_user(cq, user)
    if user is None:
        return
    data = await state.get_data()
    task_id = data.get("task_id")
    await state.clear()
    if task_id is None:
        await cq.answer("Состояние утеряно. Повтори.", show_alert=True)
        return

    await _do_complete(cq=cq, bot=bot, task_id=int(task_id), user=user, comment=None, files=[])


@router.callback_query(
    StateFilter(TaskComplete.waiting_comment),
    F.data.startswith("tcomp_abort:"),
)
async def cb_complete_abort(
    cq: CallbackQuery,
    state: FSMContext,
    user: User | None = None,
) -> None:
    """«❌ Отмена» — выйти из FSM, задачу не завершать."""
    data = await state.get_data()
    task_id = data.get("task_id")
    disp = data.get("display_number") or task_id
    await state.clear()
    await cq.answer("Завершение отменено.", show_alert=False)
    if isinstance(cq.message, Message):
        text = (
            f"❌ Завершение задачи #{disp} отменено. Задача осталась в работе."
            if disp is not None
            else "❌ Завершение отменено. Задача осталась в работе."
        )
        try:
            await cq.message.edit_text(text)
        except Exception:  # noqa: BLE001
            pass


async def _do_complete(
    *,
    cq: CallbackQuery,
    bot: Bot,
    task_id: int,
    user: User,
    comment: str | None,
    files: list[dict[str, Any]] | None = None,
) -> None:
    try:
        result = await TaskActionsService.complete(
            bot=bot,
            task_id=task_id,
            user=user,
            result_comment=comment,
            result_files=files,
        )
    except InvalidStatusTransition:
        await cq.answer(
            "Действие уже невозможно — задача в другом статусе.",
            show_alert=True,
        )
        return
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("task_complete failed for #{}: {}", task_id, exc)
        await cq.answer("Ошибка. Попробуй ещё раз позже.", show_alert=True)
        return

    await cq.answer(result.user_message, show_alert=not result.success)

    # Заменяем устаревшее сообщение FSM (с инструкцией и кнопками «Готово/
    # Без отчёта/Отмена») на финальный статус. Иначе юзер видит на экране
    # старое сообщение с активными кнопками и думает что ничего не
    # произошло — это и есть «фриз» с его стороны, хотя бэк давно отработал.
    if result.success and isinstance(cq.message, Message):
        disp = result.task.display_number if result.task is not None else task_id
        try:
            await cq.message.edit_text(
                f"📨 Отчёт по задаче #{disp} отправлен постановщику на согласование. Ждём решения."
            )
        except Exception:  # noqa: BLE001
            pass


# ──────────────────────────────────────────────────────────────────────
# task_approve / task_rework — приёмка результата постановщиком (Этап Б)
# ──────────────────────────────────────────────────────────────────────


async def _strip_approval_kb(cq: CallbackQuery) -> None:
    """Снимает кнопки [Согласовано/На доработку] с сообщения постановщика,
    чтобы он не нажал второй раз и не получил «уже невозможно». Работает
    и для текстовых сообщений, и для медиа (caption-сообщений).
    """
    if not isinstance(cq.message, Message):
        return
    try:
        await cq.message.edit_reply_markup(reply_markup=None)
    except Exception as exc:  # noqa: BLE001
        logger.debug("strip approval kb failed (probably already gone): {}", exc)


@router.callback_query(F.data.startswith("task_approve:"))
async def cb_approve(cq: CallbackQuery, bot: Bot, user: User | None = None) -> None:
    user = await _ensure_user(cq, user)
    if user is None:
        return
    task_id = _parse_task_id(cq.data)
    if task_id is None:
        await cq.answer("Некорректная задача.", show_alert=True)
        return

    try:
        result = await TaskActionsService.approve(bot=bot, task_id=task_id, user=user)
    except InvalidStatusTransition:
        await cq.answer(
            "Действие уже невозможно — задача в другом статусе.",
            show_alert=True,
        )
        return
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("task_approve failed for #{}: {}", task_id, exc)
        await cq.answer("Ошибка. Попробуй ещё раз позже.", show_alert=True)
        return

    if result.success:
        await _strip_approval_kb(cq)
    await cq.answer(result.user_message, show_alert=not result.success)
    # Финальное подтверждение постановщику отдельным сообщением — чтобы было
    # явно видно «согласовано», а не только пропавшие кнопки. Шлём после
    # успеха; старое сообщение (с отчётом исполнителя) не трогаем — оно
    # может быть caption у медиа, edit там съел бы файл.
    if result.success and isinstance(cq.message, Message):
        try:
            title_part = ""
            display = task_id
            if result.task is not None:
                if result.task.display_number:
                    display = result.task.display_number
                if result.task.title:
                    t = result.task.title
                    if len(t) > 60:
                        t = t[:59] + "…"
                    title_part = f" «{t}»"
            await cq.message.answer(f"✅ Задача #{display}{title_part} согласована и закрыта.")
        except Exception as exc:  # noqa: BLE001
            logger.debug("approve confirmation send failed: {}", exc)


MAX_REWORK_COMMENT_LEN = 4096
MAX_REWORK_FILES = 10


@router.callback_query(F.data.startswith("task_rework:"))
async def cb_rework_start(cq: CallbackQuery, state: FSMContext, user: User | None = None) -> None:
    """Этап В: «✏️ На доработку» — открывает FSM RequestRework.
    Постановщик обязан написать текст с правками, опционально приложить файлы.
    Только после нажатия «✅ Отправить на доработку» вызывается сервис.
    """
    user = await _ensure_user(cq, user)
    if user is None:
        return
    task_id = _parse_task_id(cq.data)
    if task_id is None:
        await cq.answer("Некорректная задача.", show_alert=True)
        return

    # Координаты исходного сообщения постановщика — для восстановления
    # approval-kb при отмене FSM (см. _restore_approval_kb_from_state).
    approval_chat_id: int | None = None
    approval_message_id: int | None = None
    if isinstance(cq.message, Message):
        approval_chat_id = cq.message.chat.id
        approval_message_id = cq.message.message_id

    async with async_session_factory() as session:
        disp = await TasksRepository.get_display_number(session, task_id)
    disp = disp or task_id
    await state.set_state(RequestRework.waiting_comment)
    await state.update_data(
        task_id=task_id,
        display_number=disp,
        started_at=time.monotonic(),
        comment=None,
        files=[],
        approval_chat_id=approval_chat_id,
        approval_message_id=approval_message_id,
    )
    # Снимаем approval-kb с сообщения, чтобы постановщик не нажал второй
    # раз пока заполняет правки. Если он передумает — «❌ Отмена» в FSM
    # вернёт клавиатуру через _restore_approval_kb_from_state.
    await _strip_approval_kb(cq)
    if isinstance(cq.message, Message):
        await cq.message.answer(
            f"<b>Возврат задачи #{disp} на доработку</b>\n\n"
            "<b>Шаг 1.</b> Напиши текстом, что именно нужно поправить — "
            "это сообщение уйдёт исполнителю в личку. Без текста "
            "отправить нельзя.\n"
            "<b>Шаг 2 (опционально).</b> Приложи файлы-референсы: нажми 📎 "
            "(скрепка) слева от поля ввода → «Файл» или «Фото/видео» → "
            "выбери файл → отправь. Можно несколько.\n"
            "<b>Шаг 3.</b> Нажми «✅ Отправить на доработку» ниже.\n\n"
            "Передумал — «❌ Отмена».\n"
            f"Лимиты: текст до {MAX_REWORK_COMMENT_LEN} символов, "
            f"вложений до {MAX_REWORK_FILES}.",
            reply_markup=build_rework_finalize_kb(task_id),
        )
    await cq.answer()


@router.message(
    StateFilter(RequestRework.waiting_comment),
    F.document | F.photo | F.video | F.animation,
)
async def msg_rework_collect_file(
    message: Message,
    state: FSMContext,
    user: User | None = None,
    album: list[Message] | None = None,
) -> None:
    if user is None or not user.is_active:
        await message.answer("⏳ Аккаунт не активирован, действие невозможно.")
        await state.clear()
        return
    if not await _fsm_is_fresh(state):
        await state.clear()
        await message.answer(
            "Время ожидания истекло. Открой задачу и нажми «На доработку» ещё раз."
        )
        return

    incoming: list[Message] = album if album else [message]
    data = await state.get_data()
    files: list[dict[str, Any]] = list(data.get("files") or [])
    current_count = len(files)

    accepted, oversize, overflow = collect_files_from_messages(
        incoming,
        extractor=_file_from_message,
        current_count=current_count,
        max_count=MAX_REWORK_FILES,
    )
    if accepted:
        files.extend(accepted)
        await state.update_data(files=files)

    caption = (incoming[0].caption or "").strip() if incoming else ""
    if caption:
        cur = (data.get("comment") or "").strip()
        merged = (cur + ("\n" if cur else "") + caption)[:MAX_REWORK_COMMENT_LEN]
        await state.update_data(comment=merged)

    notes: list[str] = []
    if oversize:
        notes.append(f"⚠️ {oversize} файл(ов) больше 20 МБ — не принял.")
    if overflow:
        notes.append(
            f"⚠️ {overflow} файл(ов) не вошли в лимит {MAX_REWORK_FILES}. "
            "Если правок достаточно — нажми «✅ Отправить на доработку»."
        )

    fresh = await state.get_data()
    summary = _rework_accumulator_summary(
        comment=fresh.get("comment"), files=list(fresh.get("files") or [])
    )
    task_id = fresh.get("task_id")
    kb = build_rework_finalize_kb(int(task_id)) if task_id is not None else None
    body = "\n\n".join([f"📎 Принял. {summary}", *notes])
    await message.answer(body, reply_markup=kb)


@router.message(
    StateFilter(RequestRework.waiting_comment),
    F.text & ~F.text.startswith("/"),
)
async def msg_rework_collect_text(
    message: Message, state: FSMContext, user: User | None = None
) -> None:
    if user is None or not user.is_active:
        await message.answer("⏳ Аккаунт не активирован, действие невозможно.")
        await state.clear()
        return
    if not await _fsm_is_fresh(state):
        await state.clear()
        await message.answer(
            "Время ожидания истекло. Открой задачу и нажми «На доработку» ещё раз."
        )
        return

    text = (message.text or "").strip()
    if not text:
        return
    if len(text) > MAX_REWORK_COMMENT_LEN:
        await message.answer(f"Текст длиннее {MAX_REWORK_COMMENT_LEN} символов. Сократи и повтори.")
        return

    data = await state.get_data()
    await state.update_data(comment=text)
    summary = _rework_accumulator_summary(comment=text, files=list(data.get("files") or []))
    task_id = data.get("task_id")
    kb = build_rework_finalize_kb(int(task_id)) if task_id is not None else None
    await message.answer(f"💬 Принял. {summary}", reply_markup=kb)


def _rework_accumulator_summary(*, comment: str | None, files: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    if comment:
        parts.append(f"правки ✓ ({len(comment)} симв.)")
    else:
        parts.append("правки ✗ (обязательны)")
    if files:
        parts.append(f"вложений ✓ ({len(files)})")
    return "Сейчас: " + ", ".join(parts) + "."


@router.callback_query(
    StateFilter(RequestRework.waiting_comment),
    F.data.startswith("trework_send:"),
)
async def cb_rework_send(
    cq: CallbackQuery, state: FSMContext, bot: Bot, user: User | None = None
) -> None:
    user = await _ensure_user(cq, user)
    if user is None:
        return
    data = await state.get_data()
    task_id = data.get("task_id")
    disp = data.get("display_number") or task_id
    comment = (data.get("comment") or "").strip()
    files: list[dict[str, Any]] = list(data.get("files") or [])

    if not comment:
        await cq.answer(
            "Без текста с правками отправить нельзя. Напиши, что поправить.",
            show_alert=True,
        )
        return
    if task_id is None:
        await state.clear()
        await cq.answer("Состояние утеряно. Повтори через карточку задачи.", show_alert=True)
        return

    await state.clear()
    try:
        result = await TaskActionsService.request_rework(
            bot=bot,
            task_id=int(task_id),
            user=user,
            comment=comment,
            rework_files=files,
        )
    except InvalidStatusTransition:
        await cq.answer(
            "Действие уже невозможно — задача в другом статусе.",
            show_alert=True,
        )
        return
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("task_rework send failed for #{}: {}", task_id, exc)
        await cq.answer("Ошибка. Попробуй ещё раз позже.", show_alert=True)
        return

    await cq.answer(result.user_message, show_alert=not result.success)
    if result.success and isinstance(cq.message, Message):
        try:
            await cq.message.edit_text(f"✏️ Задача #{disp} возвращена исполнителю на доработку.")
        except Exception:  # noqa: BLE001
            pass


@router.callback_query(
    StateFilter(RequestRework.waiting_comment),
    F.data.startswith("trework_abort:"),
)
async def cb_rework_abort(
    cq: CallbackQuery,
    state: FSMContext,
    bot: Bot,
    user: User | None = None,
) -> None:
    data = await state.get_data()
    await _restore_approval_kb_from_state(bot, data)
    await state.clear()
    await cq.answer("Возврат на доработку отменён.", show_alert=False)
    if isinstance(cq.message, Message):
        try:
            await cq.message.edit_text(
                "Возврат на доработку отменён. Задача осталась на согласовании — "
                "кнопки «✅ Согласовано» и «✏️ На доработку» снова доступны на "
                "исходном сообщении с отчётом."
            )
        except Exception:  # noqa: BLE001
            pass


# ──────────────────────────────────────────────────────────────────────
# task_comment — стартует FSM TaskComment.waiting_text
# ──────────────────────────────────────────────────────────────────────


@router.callback_query(F.data.startswith("task_comment:"))
async def cb_comment_start(
    cq: CallbackQuery,
    state: FSMContext,
    user: User | None = None,
) -> None:
    user = await _ensure_user(cq, user)
    if user is None:
        return
    task_id = _parse_task_id(cq.data)
    if task_id is None:
        await cq.answer("Некорректная задача.", show_alert=True)
        return

    async with async_session_factory() as session:
        disp = await TasksRepository.get_display_number(session, task_id)
    disp = disp or task_id
    await state.set_state(TaskComment.waiting_text)
    await state.update_data(task_id=task_id, display_number=disp, started_at=time.monotonic())
    if isinstance(cq.message, Message):
        cancel_kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="❌ Отмена",
                        callback_data=CB_FSM_CANCEL,
                    )
                ]
            ]
        )
        await cq.message.answer(
            f"Введите комментарий к задаче #{disp} следующим сообщением.",
            reply_markup=cancel_kb,
        )
    await cq.answer()


@router.message(StateFilter(TaskComment.waiting_text), F.text)
async def msg_comment_text(
    message: Message,
    state: FSMContext,
    bot: Bot,
    user: User | None = None,
) -> None:
    if user is None or not user.is_active:
        await message.answer("⏳ Аккаунт не активирован, действие невозможно.")
        await state.clear()
        return
    if not await _fsm_is_fresh(state):
        await state.clear()
        await message.answer(
            "Время ожидания комментария истекло. Откройте задачу и нажмите «Комментарий» ещё раз."
        )
        return
    text = (message.text or "").strip()
    data = await state.get_data()
    task_id = data.get("task_id")
    await state.clear()
    if task_id is None:
        await message.answer("Состояние утеряно. Повтори через карточку задачи.")
        return

    try:
        result = await TaskActionsService.add_comment(
            bot=bot, task_id=int(task_id), user=user, text=text
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("task_comment failed for #{}: {}", task_id, exc)
        await message.answer("Ошибка. Попробуй ещё раз позже.")
        return

    await message.answer(result.user_message)


# ──────────────────────────────────────────────────────────────────────
# Отмена внутри FSM TaskComplete / TaskComment / RequestRework
#
# Юзер видит inline-кнопку «❌ Отмена» под промптом ввода. Старый
# slash /cancel оставлен как тихий fallback — без упоминаний в UI.
# ──────────────────────────────────────────────────────────────────────


CB_FSM_CANCEL = "ta:fsm_cancel"


async def _exit_action_fsm(
    target: Message,
    state: FSMContext,
    user: User | None,
    bot: Bot,
) -> None:
    # Если выходим из RequestRework — вернуть на исходное сообщение
    # approval-kb, иначе постановщик потеряет возможность согласовать
    # или повторно отправить задачу на доработку (см. _strip_approval_kb).
    data = await state.get_data()
    await _restore_approval_kb_from_state(bot, data)
    await state.clear()
    kb = None
    if user is not None and user.is_active:
        try:
            kb = build_main_menu(UserRole(user.role))
        except Exception:  # noqa: BLE001
            kb = None
    await target.answer("❌ Действие отменено.", reply_markup=kb)


@router.message(
    StateFilter(
        TaskComplete.waiting_comment,
        TaskComment.waiting_text,
        RequestRework.waiting_comment,
    ),
    Command("cancel"),
)
async def cmd_cancel_action_fsm(
    message: Message,
    state: FSMContext,
    bot: Bot,
    user: User | None = None,
) -> None:
    await _exit_action_fsm(message, state, user, bot)


@router.callback_query(
    StateFilter(
        TaskComplete.waiting_comment,
        TaskComment.waiting_text,
        RequestRework.waiting_comment,
    ),
    F.data == CB_FSM_CANCEL,
)
async def cb_cancel_action_fsm(
    cq: CallbackQuery,
    state: FSMContext,
    bot: Bot,
    user: User | None = None,
) -> None:
    if isinstance(cq.message, Message):
        await _exit_action_fsm(cq.message, state, user, bot)
    await cq.answer()


# ──────────────────────────────────────────────────────────────────────
# DM-fallback: исполнитель шлёт файл/текст в личку без активной FSM
# ──────────────────────────────────────────────────────────────────────
# Самая частая ошибка пользователя: после возврата на доработку он
# присылает файл боту до того, как нажал «✅ Завершить». Без FSM
# msg_complete_collect_file не сработает, файл «теряется». Этот
# handler ловит такие случаи в DM-чате (только private), находит
# единственную in_progress-задачу исполнителя и автоматически открывает
# TaskComplete c уже прикреплённым файлом/текстом.

_DM_BTN_LABELS = {
    "➕ Создать задачу",
    "📁 Архив",
    "👥 Состав",
    "📊 Статистика",
    "👤 Кабинет",
    "🤖 Спросить AI",
}


async def _open_complete_fsm_with_seed(
    *,
    message: Message,
    state: FSMContext,
    task_id: int,
    seed_file: dict[str, Any] | None = None,
    seed_files: list[dict[str, Any]] | None = None,
    seed_text: str | None,
) -> None:
    """Открывает FSM TaskComplete для task_id, сразу кладя seed-данные.

    seed_files (batch из альбома) приоритетнее одиночного seed_file —
    используется при DM-fallback на альбом из 2+ файлов.
    """
    files: list[dict[str, Any]] = []
    if seed_files:
        files.extend(seed_files)
    elif seed_file is not None:
        files.append(seed_file)
    async with async_session_factory() as session:
        disp = await TasksRepository.get_display_number(session, task_id)
    disp = disp or task_id
    await state.set_state(TaskComplete.waiting_comment)
    await state.update_data(
        task_id=task_id,
        display_number=disp,
        started_at=time.monotonic(),
        comment=(seed_text or None),
        files=files,
    )
    parts: list[str] = []
    if files:
        parts.append(f"файлов ✓ ({len(files)})")
    if seed_text:
        parts.append(f"комментарий ✓ ({len(seed_text)} симв.)")
    seed_summary = ", ".join(parts) if parts else "пусто"
    await message.answer(
        f"📨 Принял для задачи #{disp} ({seed_summary}).\n"
        "Можно прислать ещё текст и файлы по одному сообщению, либо нажми "
        "«✅ Готово, завершить» — и отчёт уйдёт постановщику.",
        reply_markup=build_complete_skip_kb(task_id),
    )


@router.message(
    StateFilter(default_state),
    F.chat.type == "private",
    F.document | F.photo | F.video | F.animation,
)
async def msg_dm_file_fallback(
    message: Message,
    state: FSMContext,
    user: User | None = None,
    album: list[Message] | None = None,
) -> None:
    """Файл/фото/видео в DM без FSM → пытаемся подхватить как отчёт.

    На альбоме из 2+ файлов middleware отдаёт список — все вкладываем в
    seed (до лимита MAX_RESULT_FILES). Каждый файл больше 20 МБ
    отбрасываем.
    """
    if user is None or not user.is_active:
        return
    incoming: list[Message] = album if album else [message]
    accepted, oversize, overflow = collect_files_from_messages(
        incoming,
        extractor=_file_from_message,
        current_count=0,
        max_count=MAX_RESULT_FILES,
    )
    if not accepted:
        return
    async with async_session_factory() as session:
        active = await TasksRepository.list_active_assigned_to(
            session, assignee_id=user.id, limit=10
        )
    in_progress = [t for t in active if t.status == "in_progress"]
    if not in_progress:
        await message.answer(
            "У тебя сейчас нет задач в работе. Если хотел отправить отчёт — "
            "открой задачу в карточке и нажми «✅ Завершить»."
        )
        return
    if len(in_progress) > 1:
        titles = "\n".join(f"#{t.display_number} {t.title}" for t in in_progress[:5])
        await message.answer(
            "У тебя несколько задач в работе:\n"
            f"{titles}\n\n"
            "Открой нужную в карточке (в топике отдела или через 👤 Кабинет) "
            "и нажми «✅ Завершить» — тогда я приму файл к ней.",
        )
        return
    target = in_progress[0]
    seed_text = (incoming[0].caption or "").strip() if incoming else ""
    seed_text = seed_text or None
    await _open_complete_fsm_with_seed(
        message=message,
        state=state,
        task_id=target.id,
        seed_files=accepted,
        seed_text=seed_text,
    )
    notes: list[str] = []
    if oversize:
        notes.append(f"⚠️ {oversize} файл(ов) больше 20 МБ — не принял.")
    if overflow:
        notes.append(f"⚠️ {overflow} файл(ов) не вошли в лимит {MAX_RESULT_FILES}.")
    if notes:
        await message.answer("\n".join(notes))
