"""
FSM создания задачи (Этап 4).

Запускается ReplyKeyboard-кнопкой «➕ Создать задачу» из главного меню.
Отмена — inline-кнопкой «❌ Отменить» в любой шаг (slash /cancel оставлен
как silent fallback, не упоминается в UI).

Состояния (см. app/bot/states.py::CreateTask):
  department → title → priority → deadline → description → files → preview

При подтверждении в `preview` вызывается TaskService.create_task,
который в одной транзакции пишет в tasks/task_files/task_history
и (если настроено) постит карточку в топик отдела + зеркало в
«Руководство», проставляя dept_message_id и arch_message_id.
"""

import asyncio
from datetime import datetime, timedelta, timezone

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from loguru import logger

from app.bot.keyboards.create_task import (
    CB_DESC_SKIP_BACK,
    CB_DESC_SKIP_CONFIRM,
    DESIGN_FORMAT_BUTTONS,
    PRIORITY_BUTTONS,
    build_dept_kb,
    build_description_kb,
    build_description_skip_confirm_kb,
    build_design_format_kb,
    build_design_reference_kb,
    build_files_kb,
    build_preview_kb,
    build_priority_kb,
    build_text_step_nav_kb,
)
from app.bot.keyboards.main_menu import (
    BTN_AI,
    BTN_ARCHIVE,
    BTN_TEAM,
    BTN_CREATE_TASK,
    BTN_STATS,
)
from app.bot.states import CreateTask
from app.bot.utils.file_extract import (
    collect_files_from_messages,
    file_full_from_message,
)
from app.bot.utils.render import render_preview
from app.bot.utils.time import format_dt_local, now_utc, parse_user_datetime
from app.db.base import async_session_factory
from app.db.enums import TaskPriority
from app.db.models import User
from app.db.repositories.departments import DepartmentsRepository
from app.services.task_service import TaskService

router = Router(name="create_task")

MAX_FILES_PER_TASK = 10
MIN_TITLE_LEN = 5
MAX_TITLE_LEN = 1024
DEADLINE_BUFFER = timedelta(minutes=5)
PRIORITY_VALUES: frozenset[str] = frozenset({v for _, v in PRIORITY_BUTTONS})

DEPT_CODE_DESIGNERS = "designers"
DESIGN_FIELD_MIN_LEN = 2
DESIGN_FIELD_MAX_LEN = 4096
DESIGN_REFERENCE_MAX_LEN = 4096

# Если последнее сообщение пользователя длиннее этого порога — следующее
# prompt-сообщение бота отправляем как новое (а не редактируем старое
# «живое» сообщение), чтобы оно появилось внизу чата под юзер-ответом.
# Иначе при длинных инпутах prompt уезжает вверх и пользователь его не
# видит — выглядит как «бот замер».
LONG_USER_INPUT_THRESHOLD = 400

MAIN_MENU_TEXTS: frozenset[str] = frozenset(
    {BTN_CREATE_TASK, BTN_ARCHIVE, BTN_TEAM, BTN_STATS, BTN_AI}
)


async def _safe_edit_text(message: Message, text: str, **kwargs) -> None:
    """edit_text с проглатыванием TelegramBadRequest (старое сообщение / тот же текст)."""
    try:
        await message.edit_text(text, **kwargs)
    except TelegramBadRequest as exc:
        logger.debug("edit_text skipped: {}", exc)


PROMPT_KEY = "prompt_msg_id"


async def _show_step(
    *,
    bot: Bot,
    chat_id: int,
    state: FSMContext,
    text: str,
    reply_markup=None,
    force_new: bool = False,
) -> None:
    """Единое «живое» prompt-сообщение всей FSM CreateTask.

    Если в state есть prompt_msg_id — пытаемся `edit_message_text` на нём.
    Если редактирование невозможно (старое сообщение, медиа, удалено) —
    отправляем новое сообщение и обновляем prompt_msg_id в state. Это
    убирает «графический мусор» от множества промежуточных сообщений:
    в чате остаётся одно «живое» сообщение бота, которое меняется по
    шагам, плюс собственные ответы пользователя (их Bot API в личке
    удалять не разрешает).

    force_new=True — удалить старый prompt и отправить новое сообщение
    в самый низ чата. Нужно когда юзер только что прислал длинный
    инпут (после edit_message_text старое prompt-сообщение остаётся
    выше юзер-ответа и фактически невидимо — выглядит как «бот замер»).
    """
    data = await state.get_data()
    prompt_id = data.get(PROMPT_KEY)
    if force_new and prompt_id:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=int(prompt_id))
        except Exception as exc:  # noqa: BLE001
            logger.debug("show_step: pre-delete failed (ok): {}", exc)
        prompt_id = None
    if prompt_id:
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=int(prompt_id),
                text=text,
                reply_markup=reply_markup,
            )
            return
        except TelegramBadRequest as exc:
            # «not modified» — текст совпал, значит prompt уже актуальный, ничего не делаем
            if "not modified" in str(exc).lower():
                return
            logger.debug("show_step: edit fallback to new message: {}", exc)
            # упало по «message to edit not found» / «can't edit caption» —
            # отправим новое, обновим prompt_msg_id
    msg = await bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)
    await state.update_data(**{PROMPT_KEY: msg.message_id})


def _is_long(message: Message) -> bool:
    """Сообщение пользователя считается длинным, если перекрывает экран
    и prompt-сообщение бота при edit_message_text уезжает наверх."""
    text = message.text or ""
    return len(text) > LONG_USER_INPUT_THRESHOLD or text.count("\n") >= 4


async def _drop_prompt(bot: Bot, chat_id: int, state: FSMContext) -> None:
    """Удаляет prompt-сообщение из чата (например, при отмене/завершении,
    когда дальше показывается финальное отдельное сообщение)."""
    data = await state.get_data()
    prompt_id = data.get(PROMPT_KEY)
    if not prompt_id:
        return
    try:
        await bot.delete_message(chat_id=chat_id, message_id=int(prompt_id))
    except Exception as exc:  # noqa: BLE001
        logger.debug("drop_prompt: delete failed: {}", exc)
    await state.update_data(**{PROMPT_KEY: None})


# ---------- Точка входа: кнопка из главного меню ----------


@router.message(F.text == BTN_CREATE_TASK)
async def start_create(message: Message, state: FSMContext, user: User, bot: Bot) -> None:
    if not user.is_active:
        await message.answer("⏳ Аккаунт ещё не активирован.")
        return

    async with async_session_factory() as session:
        depts = await DepartmentsRepository.list_active(session)
    if not depts:
        await message.answer("Нет активных отделов. Обратитесь к администратору.")
        return

    await state.clear()
    await state.set_state(CreateTask.department)
    await state.update_data(files=[])
    await _show_step(
        bot=bot,
        chat_id=message.chat.id,
        state=state,
        text="Выберите отдел:",
        reply_markup=build_dept_kb(depts),
    )


# ---------- Глобальный отмена для FSM ----------


@router.message(StateFilter(CreateTask), Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext, user: User, bot: Bot) -> None:
    await _show_step(
        bot=bot,
        chat_id=message.chat.id,
        state=state,
        text="❌ Создание задачи отменено.",
        reply_markup=None,
    )
    await state.clear()


# ---------- «⬅️ Назад»: универсальный возврат на предыдущий шаг ----------


async def _render_step_prompt(
    target_message: Message,
    target_state,
    *,
    edit: bool,
) -> None:
    """
    Перерисовывает prompt указанного шага FSM с правильной inline-клавиатурой.
    Используется хэндлером «⬅️ Назад» — текст и kb совпадают с тем, что юзер
    видел при первом проходе шага.
    """
    text: str
    kb = None
    if target_state == CreateTask.department:
        async with async_session_factory() as session:
            depts = await DepartmentsRepository.list_active(session)
        text = "Выберите отдел:"
        kb = build_dept_kb(depts)
    elif target_state == CreateTask.title:
        text = "Введите название задачи (5–255 символов):"
        kb = build_text_step_nav_kb()
    elif target_state == CreateTask.priority:
        text = "Выберите важность:"
        kb = build_priority_kb()
    elif target_state == CreateTask.deadline:
        text = "Введите срок задачи.\nПримеры: «завтра 18:00», «через 2 часа», «15.05 14:00»."
        kb = build_text_step_nav_kb()
    elif target_state == CreateTask.description:
        text = "Опишите задачу: что нужно сделать, ключевые требования, ссылки. Если ТЗ совсем не нужно — нажмите «🚫 Описания не требуется»."
        kb = build_description_kb()
    elif target_state == CreateTask.design_purpose:
        text = (
            "Заполни ТЗ для дизайна.\n\n"
            "📌 <b>Назначение</b> (афиша, баннер, визитка, story, post…):"
        )
        kb = build_text_step_nav_kb()
    elif target_state == CreateTask.design_format:
        text = "🟦 <b>Формат</b> — выбери одну из кнопок:"
        kb = build_design_format_kb()
    elif target_state == CreateTask.design_reference:
        text = (
            "🎨 <b>Референс</b> — ссылка, имя файла или короткое описание. "
            "Нет примера → «Пропустить»."
        )
        kb = build_design_reference_kb()
    elif target_state == CreateTask.files:
        text = "Прикрепите файлы (по одному, до 20 МБ каждый) или нажмите «Готово»."
        kb = build_files_kb()
    else:
        return

    if edit:
        try:
            await target_message.edit_text(text, reply_markup=kb)
            return
        except TelegramBadRequest:
            pass
    await target_message.answer(text, reply_markup=kb)


@router.callback_query(StateFilter(CreateTask), F.data == "ct_back")
async def cb_back(cq: CallbackQuery, state: FSMContext) -> None:
    current = await state.get_state()
    if current is None:
        await cq.answer("Нет активного создания задачи.")
        return

    data = await state.get_data()
    is_design = data.get("department_code") == DEPT_CODE_DESIGNERS

    target = None
    if current == CreateTask.title.state:
        target = CreateTask.department
    elif current == CreateTask.priority.state:
        target = CreateTask.title
    elif current == CreateTask.deadline.state:
        target = CreateTask.priority
    elif current == CreateTask.description.state:
        # для дизайнеров «Назад» из описания возвращает в Референс,
        # для остальных — в дедлайн (как было)
        target = CreateTask.design_reference if is_design else CreateTask.deadline
    elif current == CreateTask.design_purpose.state:
        target = CreateTask.deadline
    elif current == CreateTask.design_format.state:
        target = CreateTask.design_purpose
    elif current == CreateTask.design_reference.state:
        target = CreateTask.design_format
    elif current == CreateTask.files.state:
        # после доработки 2026-05-29 дизайнеры тоже проходят шаг description,
        # поэтому «Назад» из файлов всегда ведёт в описание (для обоих веток)
        target = CreateTask.description
    elif current == CreateTask.preview.state:
        target = CreateTask.files

    if target is None:
        await cq.answer("С первого шага возврат не нужен — нажми «❌ Отменить».")
        return

    await state.set_state(target)
    if isinstance(cq.message, Message):
        await _render_step_prompt(cq.message, target, edit=True)
    await cq.answer()


@router.callback_query(StateFilter(CreateTask), F.data == "ct_cancel")
async def cb_cancel(cq: CallbackQuery, state: FSMContext, user: User) -> None:
    if isinstance(cq.message, Message):
        await _safe_edit_text(cq.message, "❌ Создание задачи отменено.")
    await state.clear()
    await cq.answer()


# ---------- Перехват главного меню во время FSM ----------


@router.message(StateFilter(CreateTask), F.text.in_(MAIN_MENU_TEXTS))
async def msg_main_menu_during_fsm(message: Message, state: FSMContext) -> None:
    """
    Юзер ткнул в reply-кнопку главного меню («📁 Архив», «📊 Статистика», …)
    во время создания задачи. Чтобы это не парсилось как title/deadline/etc.,
    перехватываем и предлагаем явно отменить.
    """
    await message.answer(
        f"Сейчас идёт создание задачи. Чтобы перейти в «{message.text}», "
        "сначала отмени текущее создание (или нажми «⬅️ Назад» для возврата на шаг).",
        reply_markup=build_text_step_nav_kb(),
    )


# ---------- Шаг 1 → 2: выбран отдел ----------


@router.callback_query(StateFilter(CreateTask.department), F.data.startswith("ct_dept:"))
async def cb_dept(cq: CallbackQuery, state: FSMContext) -> None:
    try:
        dept_id = int(cq.data.split(":", 1)[1])
    except (IndexError, ValueError):
        await cq.answer("Некорректный отдел.", show_alert=True)
        return

    async with async_session_factory() as session:
        dept = await DepartmentsRepository.get_by_id(session, dept_id)
    if dept is None or not dept.is_active:
        await cq.answer("Отдел не найден или неактивен.", show_alert=True)
        return

    await state.update_data(
        department_id=dept.id,
        department_name=dept.name,
        department_code=dept.code,
    )
    await state.set_state(CreateTask.title)
    if isinstance(cq.message, Message):
        await _safe_edit_text(
            cq.message,
            f"Отдел: <b>{dept.name}</b>\n\nВведите название задачи "
            f"({MIN_TITLE_LEN}–{MAX_TITLE_LEN} символов):",
            reply_markup=build_text_step_nav_kb(),
        )
    await cq.answer()


# ---------- Шаг 2 → 3: название ----------


@router.message(StateFilter(CreateTask.title), F.text)
async def msg_title(message: Message, state: FSMContext, bot: Bot) -> None:
    title = (message.text or "").strip()
    if not (MIN_TITLE_LEN <= len(title) <= MAX_TITLE_LEN):
        # Валидация-ошибка — короткое flash-сообщение (не основной prompt).
        await message.answer(
            f"Название должно быть от {MIN_TITLE_LEN} до {MAX_TITLE_LEN} "
            f"символов (сейчас {len(title)}). Попробуйте ещё раз."
        )
        return
    await state.update_data(title=title)
    await state.set_state(CreateTask.priority)
    await _show_step(
        bot=bot,
        chat_id=message.chat.id,
        state=state,
        text="Выберите важность:",
        reply_markup=build_priority_kb(),
        force_new=_is_long(message),
    )


# ---------- Шаг 3 → 4: важность ----------


@router.callback_query(StateFilter(CreateTask.priority), F.data.startswith("ct_pri:"))
async def cb_priority(cq: CallbackQuery, state: FSMContext) -> None:
    try:
        pri = cq.data.split(":", 1)[1]
    except IndexError:
        await cq.answer("Некорректная важность.", show_alert=True)
        return
    if pri not in PRIORITY_VALUES:
        await cq.answer("Некорректная важность.", show_alert=True)
        return

    await state.update_data(priority=pri)
    await state.set_state(CreateTask.deadline)
    if isinstance(cq.message, Message):
        await _safe_edit_text(
            cq.message,
            "Введите срок задачи.\nПримеры: «завтра 18:00», «через 2 часа», «15.05 14:00».",
            reply_markup=build_text_step_nav_kb(),
        )
    await cq.answer()


# ---------- Шаг 4 → 5: срок ----------


@router.message(StateFilter(CreateTask.deadline), F.text)
async def msg_deadline(message: Message, state: FSMContext, bot: Bot) -> None:
    dt_utc = parse_user_datetime(message.text or "")
    if dt_utc is None:
        await message.answer(
            "Не понял дату. Примеры: «завтра 18:00», «через 2 часа», «15.05 14:00»."
        )
        return
    if dt_utc < now_utc() + DEADLINE_BUFFER:
        await message.answer(
            "Срок должен быть в будущем (минимум через 5 минут). Попробуйте ещё раз."
        )
        return

    await state.update_data(deadline=dt_utc.isoformat())
    data = await state.get_data()

    if data.get("department_code") == DEPT_CODE_DESIGNERS:
        await state.set_state(CreateTask.design_purpose)
        await _show_step(
            bot=bot,
            chat_id=message.chat.id,
            state=state,
            text=(
                f"Срок принят: <b>{format_dt_local(dt_utc)}</b>\n\n"
                "ТЗ для дизайна заполняется в 3 коротких шага. "
                "Полное описание — в самом конце.\n\n"
                "📌 <b>Шаг 1 из 3 — Назначение</b>\n"
                "Что за макет? Одна короткая строка: "
                "афиша, баннер, визитка, story, post…"
            ),
            reply_markup=build_text_step_nav_kb(),
        )
        return

    await state.set_state(CreateTask.description)
    await _show_step(
        bot=bot,
        chat_id=message.chat.id,
        state=state,
        text=(
            f"Срок принят: <b>{format_dt_local(dt_utc)}</b>\n\n"
            "Опишите задачу: что нужно сделать, ключевые требования, ссылки. "
            "Если ТЗ совсем не нужно — нажмите «🚫 Описания не требуется»."
        ),
        reply_markup=build_description_kb(),
    )


# ---------- Дизайнерский подмастер (для dept.code == "designers") ----------


def _validate_design_field(text: str) -> str | None:
    """Возвращает текст для ответа об ошибке или None если ок."""
    t = text.strip()
    if len(t) < DESIGN_FIELD_MIN_LEN:
        return f"Слишком коротко (минимум {DESIGN_FIELD_MIN_LEN} символа). Попробуй ещё раз."
    if len(t) > DESIGN_FIELD_MAX_LEN:
        return f"Слишком длинно (максимум {DESIGN_FIELD_MAX_LEN} символов). Сократи."
    return None


@router.message(StateFilter(CreateTask.design_purpose), F.text)
async def msg_design_purpose(message: Message, state: FSMContext, bot: Bot) -> None:
    text = (message.text or "").strip()
    err = _validate_design_field(text)
    if err is not None:
        await message.answer(err)
        return
    await state.update_data(design_purpose=text)
    await state.set_state(CreateTask.design_format)
    await _show_step(
        bot=bot,
        chat_id=message.chat.id,
        state=state,
        text=(
            "🟦 <b>Шаг 2 из 3 — Формат</b>\n"
            "Размер/пропорции макета. Выбери кнопкой "
            "(или «✍ Свой», если нестандартный)."
        ),
        reply_markup=build_design_format_kb(),
        force_new=_is_long(message),
    )


@router.callback_query(StateFilter(CreateTask.design_format), F.data.startswith("ct_design_fmt:"))
async def cb_design_format_pick(cq: CallbackQuery, state: FSMContext) -> None:
    try:
        idx = int((cq.data or "").split(":", 1)[1])
        _, value = DESIGN_FORMAT_BUTTONS[idx]
    except (IndexError, ValueError):
        await cq.answer("Некорректный формат.", show_alert=True)
        return
    await state.update_data(design_format=value, awaiting_format_custom=False)
    await state.set_state(CreateTask.design_reference)
    if isinstance(cq.message, Message):
        await _safe_edit_text(
            cq.message,
            f"Формат: <b>{value}</b>\n\n"
            "🎨 <b>Шаг 3 из 3 — Референс</b>\n"
            "Ссылка, имя файла или короткое описание похожего макета. "
            "Если примера нет — нажми «Пропустить».",
            reply_markup=build_design_reference_kb(),
        )
    await cq.answer()


@router.callback_query(StateFilter(CreateTask.design_format), F.data == "ct_design_fmt_custom")
async def cb_design_format_custom(cq: CallbackQuery, state: FSMContext) -> None:
    """«Свой формат» — переключаем шаг в режим текстового ввода. Без перехода
    в новый state, чтобы «⬅️ Назад» по-прежнему вёл к выбору кнопок."""
    await state.update_data(awaiting_format_custom=True)
    if isinstance(cq.message, Message):
        await _safe_edit_text(
            cq.message,
            "🟦 <b>Формат</b> — опиши коротко (например: «1080×1350, 4:5, лента»).",
            reply_markup=build_text_step_nav_kb(),
        )
    await cq.answer()


@router.message(
    StateFilter(
        CreateTask.design_purpose,
        CreateTask.design_format,
        CreateTask.design_reference,
    ),
    F.photo | F.document | F.video | F.animation,
)
async def msg_design_attachment_too_early(message: Message) -> None:
    """Юзер кинул фото/файл на дизайнерском шаге описания — на этих шагах
    бот ждёт текст или нажатие кнопки. Файлы — на следующем шаге, после
    референса. Без явной подсказки сообщение проваливается молча."""
    await message.answer(
        "Сейчас идёт описание задачи — файлы можно будет приложить "
        "на следующем шаге, после «Референса». Пока ответьте текстом "
        "или нажмите кнопку."
    )


@router.message(StateFilter(CreateTask.design_format), F.text)
async def msg_design_format_custom(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    if not data.get("awaiting_format_custom"):
        # Юзер пишет текст вместо нажатия кнопки — мягко напоминаем.
        await message.answer(
            "Выбери формат кнопкой ниже или нажми «✍ Свой / описать», чтобы ввести своё значение.",
            reply_markup=build_design_format_kb(),
        )
        return
    text = (message.text or "").strip()
    err = _validate_design_field(text)
    if err is not None:
        await message.answer(err)
        return
    await state.update_data(design_format=text, awaiting_format_custom=False)
    await state.set_state(CreateTask.design_reference)
    await _show_step(
        bot=bot,
        chat_id=message.chat.id,
        state=state,
        text=(
            f"Формат: <b>{text}</b>\n\n"
            "🎨 <b>Шаг 3 из 3 — Референс</b>\n"
            "Ссылка, имя файла или короткое описание похожего макета. "
            "Если примера нет — нажми «Пропустить»."
        ),
        reply_markup=build_design_reference_kb(),
        force_new=_is_long(message),
    )


@router.callback_query(StateFilter(CreateTask.design_reference), F.data == "ct_design_ref_skip")
async def cb_design_ref_skip(cq: CallbackQuery, state: FSMContext) -> None:
    """Референс пропущен → дальше дизайнер тоже проходит общий шаг
    «Описание» (с возможностью «🚫 Описания не требуется»). Так дизайнерская
    задача может иметь дополнительный свободный текст поверх ТЗ-полей.
    """
    await state.update_data(design_reference=None)
    await state.set_state(CreateTask.description)
    if isinstance(cq.message, Message):
        await _safe_edit_text(
            cq.message,
            "Добавь свободное описание к ТЗ или нажми «🚫 Описания не требуется».",
            reply_markup=build_description_kb(),
        )
    await cq.answer()


@router.message(StateFilter(CreateTask.design_reference), F.text)
async def msg_design_reference(message: Message, state: FSMContext, bot: Bot) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer("Пусто — отправь текст/ссылку или нажми «Пропустить».")
        return
    if len(text) > DESIGN_REFERENCE_MAX_LEN:
        await message.answer(f"Слишком длинно (максимум {DESIGN_REFERENCE_MAX_LEN} символов).")
        return
    await state.update_data(design_reference=text)
    await state.set_state(CreateTask.description)
    await _show_step(
        bot=bot,
        chat_id=message.chat.id,
        state=state,
        text=(
            "📝 <b>Описание задачи (свободный текст)</b>\n"
            "Сюда можно вставить полный текст/контекст макета — без "
            "ограничений по длине строк. Или нажми «🚫 Описания не требуется»."
        ),
        reply_markup=build_description_kb(),
        force_new=_is_long(message),
    )


def _compose_design_description(data: dict) -> str:
    """Собирает многострочное описание для задачи отдела дизайнеров.

    Готовый файл всегда PNG (договорённость с редакцией), поэтому отдельного
    шага про форматы файла нет — он зашит. «Формат» в подмастере означает
    форму/соотношение картинки: Квадрат / Горизонтальный / Вертикальный /
    свободное описание.
    """
    parts: list[str] = [
        f"📌 Назначение: {data['design_purpose']}",
        f"🟦 Формат: {data['design_format']}",
        "📄 Файл: PNG",
    ]
    ref = data.get("design_reference")
    if ref:
        parts.append(f"🎨 Референс: {ref}")
    # Свободное описание после ТЗ-полей (если постановщик его написал
    # на общем шаге CreateTask.description). Если нажал «🚫 Описания
    # не требуется» — это поле None и блок не добавляется.
    extra = data.get("description")
    if extra:
        parts.append("")
        parts.append("📝 Описание:")
        parts.append(extra)
    return "\n".join(parts)


# ---------- Шаг 5 → 6: описание (текст или skip) ----------


@router.callback_query(StateFilter(CreateTask.description), F.data == "ct_desc_skip")
async def cb_desc_skip_ask(cq: CallbackQuery) -> None:
    """Первый клик «🚫 Описания не требуется» — спрашиваем подтверждение.

    Описание — главный источник претензий «вы не написали что нужно».
    Если постановщик пропускает его, заставляем явно подтвердить, чтобы
    потом не было «вы не сказали, а правки прислали 10 раз».
    """
    if isinstance(cq.message, Message):
        await _safe_edit_text(
            cq.message,
            "⚠️ <b>Точно без описания?</b>\n\n"
            "Если потом понадобятся правки, которые можно было сразу написать в "
            "описание — исполнитель будет вправе сослаться на отсутствие ТЗ. "
            "Подумай, может одной фразой указать главное?",
            reply_markup=build_description_skip_confirm_kb(),
        )
    await cq.answer()


@router.callback_query(StateFilter(CreateTask.description), F.data == CB_DESC_SKIP_CONFIRM)
async def cb_desc_skip_confirm(cq: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(description=None)
    await state.set_state(CreateTask.files)
    if isinstance(cq.message, Message):
        await _safe_edit_text(
            cq.message,
            "Без описания.\n\n"
            "📎 <b>Шаг 6 — файлы</b>\n"
            "Прикрепи до 10 файлов <b>одного типа за раз</b>:\n"
            "• фото и видео можно вместе (один альбом)\n"
            "• документы (PDF, .docx и т.п.) — отдельной отправкой\n\n"
            "Нужно смешать типы — отправляй в несколько прикреплений. "
            "Или нажми «Готово».",
            reply_markup=build_files_kb(),
        )
    await cq.answer()


@router.callback_query(StateFilter(CreateTask.description), F.data == CB_DESC_SKIP_BACK)
async def cb_desc_skip_back(cq: CallbackQuery) -> None:
    """Юзер передумал — возвращаем шаг ввода описания."""
    if isinstance(cq.message, Message):
        await _safe_edit_text(
            cq.message,
            "Опишите задачу или нажмите «🚫 Описания не требуется».",
            reply_markup=build_description_kb(),
        )
    await cq.answer()


@router.message(StateFilter(CreateTask.description), F.text)
async def msg_description(message: Message, state: FSMContext, bot: Bot) -> None:
    desc = (message.text or "").strip()
    if not desc:
        await message.answer(
            "Описание пустое. Введите текст или нажмите «🚫 Описания не требуется»."
        )
        return
    await state.update_data(description=desc)
    await state.set_state(CreateTask.files)
    await _show_step(
        bot=bot,
        chat_id=message.chat.id,
        state=state,
        text=(
            "📎 <b>Шаг 6 — файлы</b>\n"
            "Прикрепи до 10 файлов <b>одного типа за раз</b> (до 20 МБ каждый):\n"
            "• фото и видео можно вместе (один альбом)\n"
            "• документы (PDF, .docx и т.п.) — отдельной отправкой\n\n"
            "Нужно смешать типы — отправляй в несколько прикреплений. "
            "Или нажми «Готово»."
        ),
        reply_markup=build_files_kb(),
        force_new=_is_long(message),
    )


# ---------- Шаг 6: файлы ----------


@router.message(
    StateFilter(CreateTask.files),
    F.document | F.photo | F.video | F.animation,
)
async def msg_attach(
    message: Message,
    state: FSMContext,
    bot: Bot,
    album: list[Message] | None = None,
) -> None:
    """Единый накопитель файлов (document/photo/video/animation).

    Если пришёл альбом — AlbumMiddleware кладёт список в `album`, обрабатываем
    батчем одной FSM-транзакцией. Иначе работаем с `message` как одиночный.
    Раньше были 4 раздельных handler'а с гонкой на read-modify-write FSM:
    альбом из N файлов → N concurrent tasks → последний write побеждал,
    остальные файлы терялись. Теперь FSM-запись одна на весь батч.
    """
    incoming: list[Message] = album if album else [message]
    data = await state.get_data()
    files = list(data.get("files") or [])
    current_count = len(files)

    accepted, oversize, overflow = collect_files_from_messages(
        incoming,
        extractor=file_full_from_message,
        current_count=current_count,
        max_count=MAX_FILES_PER_TASK,
    )
    if accepted:
        files.extend(accepted)
        await state.update_data(files=files)

    notes: list[str] = []
    if oversize:
        notes.append(
            f"⚠️ {oversize} файл(ов) больше 20 МБ — Telegram Bot API такие не пропускает, "
            "не принял. Приложи ссылку на облако в описании или возьми поменьше."
        )
    if overflow:
        notes.append(
            f"⚠️ {overflow} файл(ов) не вошли в лимит {MAX_FILES_PER_TASK}. "
            "Удали лишние через «Готово»→«Назад» или заверши задачу с тем что есть."
        )
    if not accepted and not notes:
        return

    summary = f"📎 Прикреплено файлов: {len(files)}."
    if accepted:
        summary += " Можно добавить ещё или нажать «Готово»."
    body = "\n\n".join([summary, *notes])
    await _show_step(
        bot=bot,
        chat_id=message.chat.id,
        state=state,
        text=body,
        reply_markup=build_files_kb(),
    )


@router.message(
    StateFilter(CreateTask.files),
    ~F.text,
    ~F.document,
    ~F.photo,
    ~F.video,
    ~F.animation,
)
async def msg_unsupported_attachment(message: Message) -> None:
    """
    Аудио, голосовые, стикеры, видеокружки и прочее, что не поддерживаем.
    Без этой ветки юзер кидает голосовое и не понимает, почему «ничего не произошло».
    """
    await message.answer(
        "Этот тип вложения не поддерживается. Принимаю: фото, документы "
        "(.ai .doc .xls .pdf .ps .pptx .ttf .svg .png и т.п.), видео .mp4, GIF. "
        "Голос/кружок/стикер пришли как файл, если нужно их сохранить.",
        reply_markup=build_files_kb(),
    )


@router.callback_query(StateFilter(CreateTask.files), F.data == "ct_files_done")
async def cb_files_done(cq: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    if isinstance(cq.message, Message):
        await _show_preview(bot, cq.message.chat.id, state)
    await cq.answer()


async def _show_preview(bot: Bot, chat_id: int, state: FSMContext) -> None:
    data = await state.get_data()
    dt_utc = datetime.fromisoformat(data["deadline"]).astimezone(timezone.utc)
    files = data.get("files", [])

    if data.get("department_code") == DEPT_CODE_DESIGNERS:
        description = _compose_design_description(data)
    else:
        description = data.get("description")

    preview_text = render_preview(
        title=data["title"],
        department_name=data.get("department_name", "—"),
        priority=data["priority"],
        deadline_utc=dt_utc,
        description=description,
        files_count=len(files),
    )
    await state.set_state(CreateTask.preview)
    await _show_step(
        bot=bot,
        chat_id=chat_id,
        state=state,
        text=preview_text,
        reply_markup=build_preview_kb(),
    )


# ---------- Шаг 7: preview → отправка / редактирование ----------


@router.callback_query(StateFilter(CreateTask.preview), F.data == "ct_send")
async def cb_send(cq: CallbackQuery, state: FSMContext, user: User, bot: Bot) -> None:
    data = await state.get_data()
    dt_utc = datetime.fromisoformat(data["deadline"]).astimezone(timezone.utc)
    if data.get("department_code") == DEPT_CODE_DESIGNERS:
        description = _compose_design_description(data)
    else:
        description = data.get("description")
    try:
        task = await TaskService.create_task(
            bot=bot,
            creator=user,
            department_id=data["department_id"],
            title=data["title"],
            priority=TaskPriority(data["priority"]),
            deadline_utc=dt_utc,
            description=description,
            files=data.get("files", []),
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — финальный edge перед UI
        logger.exception("create_task failed: {}", exc)
        if isinstance(cq.message, Message):
            await _safe_edit_text(
                cq.message,
                "❌ Ошибка загрузки. Попробуйте позже или обратитесь к разработчику.",
            )
        await cq.answer()
        return

    # Публикация в чат идёт через outbox; даже если TG сейчас недоступен,
    # фоновый worker её доставит. Юзеру объясняем что произошло с задачей —
    # без этого он не понимает, ушла ли карточка дальше и кто её ждёт.
    dept_name = data.get("department_name") or "выбранный отдел"
    if isinstance(cq.message, Message):
        await _safe_edit_text(
            cq.message,
            f"✅ Задача #{task.display_number} создана.\n"
            f"📨 Карточка отправлена в личку всем активным участникам "
            f"направления «{dept_name}». Когда кто-то примет её в работу — "
            f"тебе придёт уведомление.",
        )
    await state.clear()
    await cq.answer()


@router.callback_query(StateFilter(CreateTask.preview), F.data == "ct_edit")
async def cb_edit(cq: CallbackQuery, state: FSMContext) -> None:
    async with async_session_factory() as session:
        depts = await DepartmentsRepository.list_active(session)
    await state.clear()
    await state.set_state(CreateTask.department)
    await state.update_data(files=[])
    if isinstance(cq.message, Message):
        await _safe_edit_text(
            cq.message,
            "Начнём заново.\n\nВыберите отдел:",
            reply_markup=build_dept_kb(depts),
        )
    await cq.answer()
