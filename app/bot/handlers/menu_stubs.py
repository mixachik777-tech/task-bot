"""
Все кнопки главного меню теперь обслуживаются конкретными роутерами:
create_task, archive, team, analytics, ai_chat. Этот роутер регистрируется
ПОСЛЕДНИМ в main.py и держит fallback на текст вне FSM — чтобы юзер,
который промахнулся мимо кнопки меню или вернулся после истечения FSM-TTL,
не получал молчанку. Без этого fallback'а бот выглядит «сдохшим»
(см. инцидент @anastassss14 — после трёх неудачных попыток сократить
длинный текст FSM умерла, и любое следующее сообщение терялось без ответа).
"""

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.state import default_state
from aiogram.types import Message

from app.db.models import User

router = Router(name="menu_stubs")


@router.message(
    StateFilter(default_state),
    F.chat.type == "private",
    F.text,
)
async def fallback_text(message: Message, user: User | None = None) -> None:
    """Юзер написал текст в личке, не находясь в FSM, и текст не совпал
    ни с одной кнопкой меню (иначе сработал бы более ранний роутер).
    Подсказываем как вернуться в меню — вместо молчания."""
    if user is None or not user.is_active:
        # Ещё не активирован — у него поток onboarding. Не вмешиваемся.
        return
    await message.answer(
        "Не понял команду. Нажми кнопку меню снизу или /start, чтобы открыть его заново."
    )
