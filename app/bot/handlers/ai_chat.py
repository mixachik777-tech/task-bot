"""
Меню «🤖 Спросить AI» — диалог с Gemini-агентом в личке бота.

UX:
- Юзер жмёт BTN_AI → бот шлёт приветствие + inline-клавиатуру [очистить, закрыть].
- Юзер пишет вопрос → бот typing → агент возвращает текст → ответ юзеру.
- История диалога сохраняется (user_text + final model_text) в ai_conversations,
  обрезается до последних 20 сообщений.
- Ошибки Gemini маппятся на дружелюбные тексты, бот продолжает работать.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from loguru import logger

from app.ai.agent import GeminiAgent
from app.ai.context import UserContext
from app.ai.errors import AiRateLimit, AiTimeout, AiUnavailable
from app.ai.quota import check_and_increment
from app.bot.keyboards.main_menu import BTN_AI
from app.bot.runtime import get_redis
from app.config import settings
from app.db.base import async_session_factory
from app.db.enums import AccessStatus
from app.db.models import User
from app.db.repositories.ai_conversations import AiConversationsRepository

router = Router(name="ai_chat")

MAX_QUESTION_LEN = 4096
# Закрываем AiChat-FSM, если юзер ничего не писал N минут. Без TTL FSM
# висит в Redis вечно после каждого открытия и нагромождается у активных
# юзеров. 30 мин — компромисс между «успеть сформулировать вопрос» и
# «не накапливать мёртвые сессии».
AI_FSM_TTL_SECONDS = 30 * 60


class AiChat(StatesGroup):
    viewing = State()


_agent: GeminiAgent | None = None


def _get_agent() -> GeminiAgent:
    """Ленивая инициализация: если ключа нет — RuntimeError'ит при первом вопросе."""
    global _agent
    if _agent is None:
        _agent = GeminiAgent(api_key=settings.GEMINI_API_KEY)
    return _agent


def _kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🗑 Очистить диалог", callback_data="ai:clear"),
                InlineKeyboardButton(text="✖️ Закрыть", callback_data="ai:close"),
            ]
        ]
    )


WELCOME = (
    "🤖 AI-помощник task-bot\n\n"
    "Отвечаю на вопросы про задачи редакции. Примеры:\n"
    "• Мои просрочки\n"
    "• Что у Алёны в работе\n"
    "• Найди задачи про афишу\n"
    "• История задачи #5\n"
    "• Сформулируй задачу: афиша 9 мая на корректуру\n\n"
    "Напиши вопрос (до 4096 символов) или нажми «Закрыть»."
)


@router.message(F.chat.type == "private", F.text == BTN_AI)
async def msg_ai_open(message: Message, state: FSMContext, user: User) -> None:
    if user.access_status == AccessStatus.DENIED.value:
        await message.answer("❌ Доступ к боту отклонён.")
        return
    if user.access_status != AccessStatus.APPROVED.value or not user.is_active:
        await message.answer("⏳ Аккаунт ожидает одобрения администратором.")
        return
    if not settings.GEMINI_API_KEY:
        await message.answer("AI-помощник отключён: не задан GEMINI_API_KEY.")
        return
    import time

    await state.clear()
    await state.set_state(AiChat.viewing)
    await state.update_data(opened_at=time.monotonic())
    await message.answer(WELCOME, reply_markup=_kb(), parse_mode=None)


@router.callback_query(StateFilter(AiChat.viewing), F.data == "ai:close")
async def cb_close(cq: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    if cq.message is not None:
        try:
            await cq.message.edit_text("AI-чат закрыт.", parse_mode=None)
        except TelegramBadRequest:
            pass
    await cq.answer()


@router.callback_query(StateFilter(AiChat.viewing), F.data == "ai:clear")
async def cb_clear(cq: CallbackQuery, state: FSMContext, user: User) -> None:
    async with async_session_factory() as session:
        async with session.begin():
            await AiConversationsRepository.clear(session, user.id)
    await cq.answer("История очищена")
    if cq.message is not None:
        try:
            await cq.message.answer(
                "Готово, начинаем с чистого листа.", reply_markup=_kb(), parse_mode=None
            )
        except TelegramBadRequest:
            pass


@router.message(StateFilter(AiChat.viewing), F.chat.type == "private", F.text)
async def msg_question(message: Message, state: FSMContext, user: User) -> None:
    import time

    text = (message.text or "").strip()
    if not text:
        return
    data = await state.get_data()
    opened_at = data.get("opened_at")
    if opened_at is not None:
        try:
            if (time.monotonic() - float(opened_at)) > AI_FSM_TTL_SECONDS:
                await state.clear()
                await message.answer(
                    "AI-чат закрылся по таймауту. Открой заново через меню «🤖 Спросить AI».",
                    parse_mode=None,
                )
                return
        except (TypeError, ValueError):
            pass
    await state.update_data(opened_at=time.monotonic())
    if len(text) > MAX_QUESTION_LEN:
        await message.answer(
            f"Вопрос слишком длинный (макс {MAX_QUESTION_LEN} символов). Сократи и повтори.",
            parse_mode=None,
        )
        return

    # Per-user дневной лимит — защита общей квоты Gemini от одного активного юзера.
    allowed, used, limit = await check_and_increment(get_redis(), user.id)
    if not allowed:
        await message.answer(
            f"🛑 Дневной лимит к AI исчерпан: ты задал {limit} из {limit} "
            "разрешённых вопросов за сегодня. Счётчик обнулится в 00:00 МСК "
            "(московское время) — после этого можно снова спрашивать.",
            parse_mode=None,
        )
        return

    try:
        await message.bot.send_chat_action(chat_id=message.chat.id, action="typing")
    except Exception:  # noqa: BLE001
        pass

    try:
        agent = _get_agent()
    except RuntimeError as exc:
        await message.answer(f"AI недоступен: {exc}", parse_mode=None)
        return

    async with async_session_factory() as session:
        async with session.begin():
            conv = await AiConversationsRepository.get_or_create(session, user_id=user.id)
            history = list(conv.messages or [])

            ctx = UserContext.from_user(user)
            try:
                final = await agent.ask(session=session, ctx=ctx, user_text=text, history=history)
            except AiRateLimit:
                await message.answer(
                    "Слишком много запросов. Попробуй через минуту.", parse_mode=None
                )
                return
            except AiTimeout:
                await message.answer(
                    "Запрос занял слишком долго. Попробуй ещё раз.", parse_mode=None
                )
                return
            except AiUnavailable as exc:
                logger.warning("ai unavailable for user {}: {}", user.id, exc)
                await message.answer(
                    "Ассистент временно недоступен, попробуй позже.", parse_mode=None
                )
                return

            await AiConversationsRepository.extend(
                session,
                conv,
                [
                    {"role": "user", "parts": [{"text": text}]},
                    {"role": "model", "parts": [{"text": final}]},
                ],
            )

    await message.answer(final, reply_markup=_kb(), parse_mode=None)
