"""
Этап Г. Уточняющие вопросы исполнителя к постановщику.

Поток:
  1. Исполнитель в карточке задачи (статус in_progress) жмёт «❓ Задать
     вопрос» → cb_question_start открывает FSM TaskQuestion.waiting_text.
  2. Пользователь шлёт текст вопроса и опционально один файл/фото —
     msg_question_collect_*. Сообщения копятся в FSM.
  3. По «✉️ Отправить вопрос» — cb_question_send:
       - пишет task_history event QUESTION_ASKED с payload {text, file}
       - шлёт постановщику в DM сообщение «❓ Вопрос по задаче #N» с
         кнопкой «✏️ Ответить» (callback task_answer:{task_id}:{hist_id})
       - дублирует в зеркало «Руководство»
  4. Постановщик жмёт «✏️ Ответить» → cb_answer_start открывает FSM
     TaskQuestionAnswer.waiting_text.
  5. Постановщик пишет ответ (+ опционально файл) → cb_answer_send:
       - пишет task_history event QUESTION_ANSWERED с payload
         {text, file, question_history_id}
       - шлёт исполнителю в DM «💬 Ответ постановщика» с цитатой вопроса
       - дублирует в зеркало «Руководство»
"""

import html
import time
from typing import Any

from aiogram import Bot, F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from loguru import logger

from app.bot.keyboards.task_card import (
    build_question_answer_compose_kb,
    build_question_answer_kb,
    build_question_compose_kb,
)
from app.bot.states import TaskQuestion, TaskQuestionAnswer
from app.db.base import async_session_factory
from app.db.enums import HistoryEventType, UserRole
from app.db.models import User
from app.db.repositories.history import HistoryRepository
from app.db.repositories.tasks import TasksRepository
from app.db.repositories.users import UsersRepository
from app.services.task_actions import _try_post_event_to_lead

router = Router(name="task_question")

MAX_QUESTION_LEN = 4096
FSM_WAIT_TTL_SECONDS = 10 * 60


# ──────────────────────────────────────────────────────────────────────
# helpers
# ──────────────────────────────────────────────────────────────────────


def _parse_task_id(data: str | None) -> int | None:
    if not data:
        return None
    parts = data.split(":")
    if len(parts) < 2:
        return None
    try:
        return int(parts[1])
    except (TypeError, ValueError):
        return None


def _parse_answer_callback(data: str | None) -> tuple[int, int] | None:
    """task_answer:{task_id}:{question_history_id} → (task_id, hist_id)."""
    if not data:
        return None
    parts = data.split(":")
    if len(parts) != 3:
        return None
    try:
        return int(parts[1]), int(parts[2])
    except (TypeError, ValueError):
        return None


def _file_from_message(message: Message) -> dict[str, str | None] | None:
    if message.document is not None:
        return {
            "kind": "document",
            "file_id": message.document.file_id,
            "file_name": message.document.file_name,
        }
    if message.photo:
        return {"kind": "photo", "file_id": message.photo[-1].file_id, "file_name": None}
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


async def _fsm_is_fresh(state: FSMContext) -> bool:
    """См. task_actions._fsm_is_fresh — TTL работает как таймер бездействия,
    каждый успешный апдейт от юзера обновляет started_at. Это нужно, чтобы
    активный юзер, сокращающий длинный текст по «сократи и повтори», не
    упирался в expired через 10 мин от первого клика и не получал молчащего
    бота после state.clear()."""
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


async def _ensure_user(cq: CallbackQuery, user: User | None) -> User | None:
    if user is None:
        async with async_session_factory() as s:
            user = await UsersRepository.get_by_tg_id(s, cq.from_user.id)
    if user is None or not user.is_active:
        await cq.answer("⏳ Аккаунт не активирован.", show_alert=True)
        return None
    return user


# ──────────────────────────────────────────────────────────────────────
# Этап Г.1 — исполнитель задаёт вопрос
# ──────────────────────────────────────────────────────────────────────


@router.callback_query(F.data.startswith("task_question:"))
async def cb_question_start(
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
        task = await TasksRepository.get_by_id(session, task_id)
    if task is None:
        await cq.answer("Задача не найдена.", show_alert=True)
        return
    if task.assignee_id != user.id and user.role != UserRole.ADMIN.value:
        await cq.answer(
            "Вопрос может задать только исполнитель задачи.",
            show_alert=True,
        )
        return
    if task.status != "in_progress":
        await cq.answer(
            "Вопрос можно задавать только когда задача в работе.",
            show_alert=True,
        )
        return

    async with async_session_factory() as session:
        disp = await TasksRepository.get_display_number(session, task_id)
    disp = disp or task_id
    await state.set_state(TaskQuestion.waiting_text)
    await state.update_data(
        task_id=task_id,
        display_number=disp,
        started_at=time.monotonic(),
        text=None,
        file=None,
    )
    if isinstance(cq.message, Message):
        await cq.message.answer(
            f"<b>Вопрос по задаче #{disp}</b>\n\n"
            "Напиши текст вопроса. Можно приложить один файл или фото "
            "(дополнительные сообщения с файлами перезапишут предыдущий).\n\n"
            f"Лимит: {MAX_QUESTION_LEN} символов. Когда готово — «✉️ Отправить».",
            reply_markup=build_question_compose_kb(task_id),
        )
    await cq.answer()


@router.message(
    StateFilter(TaskQuestion.waiting_text),
    F.text & ~F.text.startswith("/"),
)
async def msg_question_collect_text(
    message: Message,
    state: FSMContext,
    user: User | None = None,
) -> None:
    if user is None or not user.is_active:
        await state.clear()
        return
    if not await _fsm_is_fresh(state):
        await state.clear()
        await message.answer(
            "Время ожидания истекло. Открой задачу и нажми «❓ Задать вопрос» ещё раз."
        )
        return
    text = (message.text or "").strip()
    if not text:
        return
    if len(text) > MAX_QUESTION_LEN:
        await message.answer(f"Текст длиннее {MAX_QUESTION_LEN} символов. Сократи и повтори.")
        return
    await state.update_data(text=text)
    data = await state.get_data()
    task_id = int(data.get("task_id") or 0)
    file_part = "файл ✓" if data.get("file") else "файла нет"
    await message.answer(
        f"💬 Принял. Текст ✓ ({len(text)} симв.), {file_part}. "
        "Можно прислать ещё или нажать «✉️ Отправить».",
        reply_markup=build_question_compose_kb(task_id),
    )


@router.message(
    StateFilter(TaskQuestion.waiting_text),
    F.document | F.photo | F.video | F.animation,
)
async def msg_question_collect_file(
    message: Message,
    state: FSMContext,
    user: User | None = None,
    album: list[Message] | None = None,
) -> None:
    if user is None or not user.is_active:
        await state.clear()
        return
    if not await _fsm_is_fresh(state):
        await state.clear()
        await message.answer(
            "Время ожидания истекло. Открой задачу и нажми «❓ Задать вопрос» ещё раз."
        )
        return
    incoming: list[Message] = album if album else [message]
    # Контракт «вопрос = текст + 1 файл» исторический. На альбом берём
    # первый файл, остальные явно отказываем — чтобы юзер понял.
    f = _file_from_message(incoming[0])
    if f is None:
        return
    await state.update_data(file=f)
    caption = (incoming[0].caption or "").strip()
    if caption:
        cur = ((await state.get_data()).get("text") or "").strip()
        merged = (cur + ("\n" if cur else "") + caption)[:MAX_QUESTION_LEN]
        await state.update_data(text=merged)
    data = await state.get_data()
    task_id = int(data.get("task_id") or 0)
    text_part = (
        f"текст ✓ ({len(data.get('text') or '')} симв.)" if data.get("text") else "текста нет"
    )
    extra = len(incoming) - 1
    extra_note = (
        f"\n\n⚠️ Вопрос принимает только 1 файл — остальные {extra} не сохранены. "
        "Если нужно больше — задай отдельный вопрос."
        if extra > 0
        else ""
    )
    await message.answer(
        f"📎 Принял файл. {text_part}. Можно прислать ещё или нажать «✉️ Отправить».{extra_note}",
        reply_markup=build_question_compose_kb(task_id),
    )


@router.callback_query(
    StateFilter(TaskQuestion.waiting_text),
    F.data.startswith("tquest_abort:"),
)
async def cb_question_abort(
    cq: CallbackQuery,
    state: FSMContext,
    user: User | None = None,
) -> None:
    await state.clear()
    if isinstance(cq.message, Message):
        try:
            await cq.message.edit_text("❌ Вопрос отменён.")
        except Exception:  # noqa: BLE001
            pass
    await cq.answer()


@router.callback_query(
    StateFilter(TaskQuestion.waiting_text),
    F.data.startswith("tquest_send:"),
)
async def cb_question_send(
    cq: CallbackQuery,
    state: FSMContext,
    bot: Bot,
    user: User | None = None,
) -> None:
    user = await _ensure_user(cq, user)
    if user is None:
        return
    data = await state.get_data()
    task_id = data.get("task_id")
    text = (data.get("text") or "").strip()
    file = data.get("file")
    if task_id is None:
        await state.clear()
        await cq.answer("Состояние утеряно. Повтори.", show_alert=True)
        return
    if not text and not file:
        await cq.answer(
            "Нужен текст вопроса или файл — иначе постановщик ничего не получит.",
            show_alert=True,
        )
        return
    await state.clear()
    task_id = int(task_id)
    disp_q: int = int(data.get("display_number") or task_id)
    task_title: str = ""
    creator_tg_id: int | None = None
    hist_id: int | None = None
    assignee_name = html.escape(user.full_name or "") or f"id={user.tg_user_id}"

    try:
        async with async_session_factory() as session:
            async with session.begin():
                task = await TasksRepository.get_full(session, task_id)
                if task is None or task.creator is None:
                    await cq.answer("Задача не найдена.", show_alert=True)
                    return
                disp_q = task.display_number or task_id
                task_title = html.escape(task.title or "")
                creator_tg_id = task.creator.tg_user_id
                hist = await HistoryRepository.log(
                    session,
                    task_id=task.id,
                    user_id=user.id,
                    event_type=HistoryEventType.QUESTION_ASKED,
                    payload={"text": text, "file": file},
                )
                hist_id = hist.id
    except Exception as exc:  # noqa: BLE001
        logger.exception("cb_question_send DB log failed (task #{}): {}", task_id, exc)
        await cq.answer(
            "Ошибка при сохранении вопроса. Попробуй ещё раз.",
            show_alert=True,
        )
        return

    # DM постановщику с кнопкой «Ответить»
    safe_text = html.escape(text) if text else ""
    header = (
        f"❓ <b>Вопрос по задаче #{disp_q}</b> «{task_title}»\n"
        f"От исполнителя: {assignee_name}\n\n"
        f"{safe_text}"
        if text
        else f"❓ <b>Вопрос по задаче #{disp_q}</b> «{task_title}»\n"
        f"От исполнителя: {assignee_name}\n\n"
        "(только файл)"
    )
    if hist_id is None or creator_tg_id is None:
        return  # ранее уже ответили error в except-блоке
    kb = build_question_answer_kb(task_id, hist_id)
    try:
        if file:
            await _send_file(bot, creator_tg_id, file, caption=header, reply_markup=kb)
        else:
            await bot.send_message(creator_tg_id, header, reply_markup=kb)
    except Exception as exc:  # noqa: BLE001
        logger.warning("question DM to creator failed (task #{}): {}", task_id, exc)
        if isinstance(cq.message, Message):
            await cq.message.answer(
                "⚠️ Не удалось доставить вопрос в личку постановщика "
                "(возможно, он не запускал бота). Попробуй задать вопрос в чате."
            )

    if isinstance(cq.message, Message):
        try:
            await cq.message.edit_text(
                f"✉️ Вопрос по задаче #{disp_q} отправлен постановщику. Ждём ответа."
            )
        except Exception:  # noqa: BLE001
            pass
    await cq.answer()

    _try_post_event_to_lead(
        bot,
        task_id,
        kind="question",
        by_user_name=assignee_name,
        comment=text or None,
        files=[file] if file else [],
    )


# ──────────────────────────────────────────────────────────────────────
# Этап Г.2 — постановщик отвечает
# ──────────────────────────────────────────────────────────────────────


@router.callback_query(F.data.startswith("task_answer:"))
async def cb_answer_start(
    cq: CallbackQuery,
    state: FSMContext,
    user: User | None = None,
) -> None:
    user = await _ensure_user(cq, user)
    if user is None:
        return
    parsed = _parse_answer_callback(cq.data)
    if parsed is None:
        await cq.answer("Некорректные данные.", show_alert=True)
        return
    task_id, question_hist_id = parsed
    async with async_session_factory() as session:
        task = await TasksRepository.get_full(session, task_id)
    if task is None or task.assignee is None:
        await cq.answer("Задача не найдена или у неё нет исполнителя.", show_alert=True)
        return
    if task.creator_id != user.id and user.role != UserRole.ADMIN.value:
        await cq.answer(
            "Ответить может только постановщик задачи.",
            show_alert=True,
        )
        return

    await state.set_state(TaskQuestionAnswer.waiting_text)
    await state.update_data(
        task_id=task_id,
        display_number=(task.display_number or task_id),
        question_history_id=question_hist_id,
        assignee_tg_id=task.assignee.tg_user_id,
        started_at=time.monotonic(),
        text=None,
        file=None,
    )
    if isinstance(cq.message, Message):
        await cq.message.answer(
            f"<b>Ответ на вопрос по задаче #{task.display_number or task_id}</b>\n\n"
            "Напиши текст ответа. Можно приложить один файл или фото.\n"
            f"Лимит: {MAX_QUESTION_LEN} символов. Когда готово — «✉️ Отправить».",
            reply_markup=build_question_answer_compose_kb(task_id),
        )
    await cq.answer()


@router.message(
    StateFilter(TaskQuestionAnswer.waiting_text),
    F.text & ~F.text.startswith("/"),
)
async def msg_answer_collect_text(
    message: Message,
    state: FSMContext,
    user: User | None = None,
) -> None:
    if user is None or not user.is_active:
        await state.clear()
        return
    if not await _fsm_is_fresh(state):
        await state.clear()
        await message.answer("Время ожидания истекло. Нажми «✏️ Ответить» на вопросе ещё раз.")
        return
    text = (message.text or "").strip()
    if not text:
        return
    if len(text) > MAX_QUESTION_LEN:
        await message.answer(f"Текст длиннее {MAX_QUESTION_LEN} символов. Сократи и повтори.")
        return
    await state.update_data(text=text)
    data = await state.get_data()
    task_id = int(data.get("task_id") or 0)
    file_part = "файл ✓" if data.get("file") else "файла нет"
    await message.answer(
        f"💬 Принял. Текст ✓ ({len(text)} симв.), {file_part}. "
        "Можно прислать ещё или нажать «✉️ Отправить».",
        reply_markup=build_question_answer_compose_kb(task_id),
    )


@router.message(
    StateFilter(TaskQuestionAnswer.waiting_text),
    F.document | F.photo | F.video | F.animation,
)
async def msg_answer_collect_file(
    message: Message,
    state: FSMContext,
    user: User | None = None,
    album: list[Message] | None = None,
) -> None:
    if user is None or not user.is_active:
        await state.clear()
        return
    if not await _fsm_is_fresh(state):
        await state.clear()
        await message.answer("Время ожидания истекло. Нажми «✏️ Ответить» на вопросе ещё раз.")
        return
    incoming: list[Message] = album if album else [message]
    # Контракт «ответ = текст + 1 файл» — берём первый из альбома,
    # остальным отказываем.
    f = _file_from_message(incoming[0])
    if f is None:
        return
    await state.update_data(file=f)
    caption = (incoming[0].caption or "").strip()
    if caption:
        cur = ((await state.get_data()).get("text") or "").strip()
        merged = (cur + ("\n" if cur else "") + caption)[:MAX_QUESTION_LEN]
        await state.update_data(text=merged)
    data = await state.get_data()
    task_id = int(data.get("task_id") or 0)
    text_part = (
        f"текст ✓ ({len(data.get('text') or '')} симв.)" if data.get("text") else "текста нет"
    )
    extra = len(incoming) - 1
    extra_note = (
        f"\n\n⚠️ Ответ принимает только 1 файл — остальные {extra} не сохранены."
        if extra > 0
        else ""
    )
    await message.answer(
        f"📎 Принял файл. {text_part}. Можно прислать ещё или нажать «✉️ Отправить».{extra_note}",
        reply_markup=build_question_answer_compose_kb(task_id),
    )


@router.callback_query(
    StateFilter(TaskQuestionAnswer.waiting_text),
    F.data.startswith("tqans_abort:"),
)
async def cb_answer_abort(
    cq: CallbackQuery,
    state: FSMContext,
    user: User | None = None,
) -> None:
    await state.clear()
    if isinstance(cq.message, Message):
        try:
            await cq.message.edit_text("❌ Ответ отменён.")
        except Exception:  # noqa: BLE001
            pass
    await cq.answer()


@router.callback_query(
    StateFilter(TaskQuestionAnswer.waiting_text),
    F.data.startswith("tqans_send:"),
)
async def cb_answer_send(
    cq: CallbackQuery,
    state: FSMContext,
    bot: Bot,
    user: User | None = None,
) -> None:
    user = await _ensure_user(cq, user)
    if user is None:
        return
    data = await state.get_data()
    task_id = data.get("task_id")
    question_hist_id = data.get("question_history_id")
    assignee_tg_id = data.get("assignee_tg_id")
    text = (data.get("text") or "").strip()
    file = data.get("file")
    if task_id is None or assignee_tg_id is None:
        await state.clear()
        await cq.answer("Состояние утеряно. Повтори.", show_alert=True)
        return
    if not text and not file:
        await cq.answer(
            "Нужен текст ответа или файл.",
            show_alert=True,
        )
        return
    await state.clear()
    task_id = int(task_id)
    disp = data.get("display_number") or task_id

    try:
        async with async_session_factory() as session:
            async with session.begin():
                await HistoryRepository.log(
                    session,
                    task_id=task_id,
                    user_id=user.id,
                    event_type=HistoryEventType.QUESTION_ANSWERED,
                    payload={
                        "text": text,
                        "file": file,
                        "question_history_id": question_hist_id,
                    },
                )
    except Exception as exc:  # noqa: BLE001
        logger.exception("cb_answer_send DB log failed (task #{}): {}", task_id, exc)
        await cq.answer(
            "Ошибка при сохранении ответа. Попробуй ещё раз.",
            show_alert=True,
        )
        return

    by_name = html.escape(user.full_name or "") or f"id={user.tg_user_id}"
    safe_text = html.escape(text) if text else ""
    header = (
        f"💬 <b>Ответ постановщика по задаче #{disp}</b>\nОт: {by_name}\n\n{safe_text}"
        if text
        else f"💬 <b>Ответ постановщика по задаче #{disp}</b>\nОт: {by_name}\n\n(только файл)"
    )
    try:
        if file:
            await _send_file(bot, int(assignee_tg_id), file, caption=header)
        else:
            await bot.send_message(int(assignee_tg_id), header)
    except Exception as exc:  # noqa: BLE001
        logger.warning("answer DM to assignee failed (task #{}): {}", task_id, exc)

    if isinstance(cq.message, Message):
        try:
            await cq.message.edit_text(f"✉️ Ответ по задаче #{disp} отправлен исполнителю.")
        except Exception:  # noqa: BLE001
            pass
    await cq.answer()

    _try_post_event_to_lead(
        bot,
        task_id,
        kind="answer",
        by_user_name=by_name,
        comment=text or None,
        files=[file] if file else [],
    )


# ──────────────────────────────────────────────────────────────────────
# helpers
# ──────────────────────────────────────────────────────────────────────


async def _send_file(
    bot: Bot,
    chat_id: int,
    file: dict[str, Any],
    *,
    caption: str | None = None,
    reply_markup: Any = None,
) -> None:
    kind = file.get("kind", "document")
    file_id = file["file_id"]
    if kind == "photo":
        await bot.send_photo(chat_id, photo=file_id, caption=caption, reply_markup=reply_markup)
    elif kind == "video":
        await bot.send_video(chat_id, video=file_id, caption=caption, reply_markup=reply_markup)
    elif kind == "animation":
        await bot.send_animation(
            chat_id, animation=file_id, caption=caption, reply_markup=reply_markup
        )
    else:
        await bot.send_document(
            chat_id, document=file_id, caption=caption, reply_markup=reply_markup
        )
