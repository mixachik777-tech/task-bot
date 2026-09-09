"""
Smoke-тесты GeminiAgent с замоканным API:

- off-topic запрос → модель сразу возвращает refusal-фразу (без tool_call)
- запрос «мои задачи» → один tool_call → final text
- запрос с двумя последовательными tool_call'ами (chain)
- превышение MAX_TOOL_ITERATIONS → AiUnavailable

Мок реализован подменой `self._client.aio.models.generate_content` —
никакого реального HTTP. Тестируется именно tool-loop, не сам Gemini.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.ai.agent import GeminiAgent
from app.ai.context import UserContext
from app.ai.errors import AiUnavailable
from app.ai.tool_registry import build_tools_object
from app.db.enums import UserRole
from app.db.repositories.departments import DepartmentsRepository
from app.db.repositories.users import UsersRepository


def _part(*, text=None, fc_name=None, fc_args=None):
    if fc_name is not None:
        return SimpleNamespace(
            text=None,
            function_call=SimpleNamespace(name=fc_name, args=fc_args or {}),
        )
    return SimpleNamespace(text=text, function_call=None)


def _response(parts: list) -> SimpleNamespace:
    return SimpleNamespace(
        candidates=[SimpleNamespace(content=SimpleNamespace(parts=parts))]
    )


def _agent_with_responses(responses: list[SimpleNamespace]) -> GeminiAgent:
    """Создаёт агент, у которого _generate возвращает responses по очереди."""
    agent = GeminiAgent.__new__(GeminiAgent)
    agent._client = None  # не используется в моках
    # реальный Tool-объект (не требует API-вызовов, нужен для валидации
    # GenerateContentConfig внутри ask())
    agent._tools_obj = build_tools_object()
    iterator = iter(responses)

    async def fake_generate(contents, config):  # noqa: ARG001
        try:
            return next(iterator)
        except StopIteration:
            raise AssertionError("agent сделал больше generate-вызовов, чем замокано")

    agent._generate = fake_generate  # type: ignore[assignment]
    return agent


async def _admin_ctx(session) -> UserContext:
    """Создаёт реального admin'а в БД и возвращает UserContext под него."""
    await DepartmentsRepository.create(
        session, code="smk_d", name="Smoke Dept", topic_id=0
    )
    u = await UsersRepository.create(
        session, tg_user_id=70001, full_name="Smoke Admin",
        is_active=True, role=UserRole.ADMIN,
    )
    return UserContext.from_user(u)


# ─── сценарии ─────────────────────────────────────────────────────────


async def test_offtopic_immediate_refusal(session):
    """Off-topic — Gemini сразу возвращает текст-отбивку без tool_call."""
    ctx = await _admin_ctx(session)
    agent = _agent_with_responses(
        [_response([_part(text="Я отвечаю только на вопросы про задачи редакции.")])]
    )
    result = await agent.ask(
        session=session, ctx=ctx,
        user_text="Какая сегодня погода в Москве?",
        history=None,
    )
    assert "только на вопросы про задачи редакции" in result


async def test_single_tool_call_then_final(session):
    """Простой кейс: tool_call → ответ инструмента → финальный текст."""
    ctx = await _admin_ctx(session)
    agent = _agent_with_responses(
        [
            _response([_part(fc_name="get_user_tasks", fc_args={"status": "in_progress"})]),
            _response([_part(text="У тебя 0 задач в работе.")]),
        ]
    )
    result = await agent.ask(
        session=session, ctx=ctx,
        user_text="Сколько у меня задач в работе?",
        history=None,
    )
    assert "0 задач" in result or "не" in result.lower()


async def test_chain_two_tool_calls(session):
    """Цепочка: сначала find_users, потом get_user_workload."""
    ctx = await _admin_ctx(session)
    # сидим юзера, которого «найдёт» find_users
    target = await UsersRepository.create(
        session, tg_user_id=70002, full_name="Ivan Petrov", tg_username="ivan",
        is_active=True, role=UserRole.ADMIN,
    )
    agent = _agent_with_responses(
        [
            _response([_part(fc_name="find_users", fc_args={"query": "Ivan"})]),
            _response(
                [_part(fc_name="get_user_workload", fc_args={"user_id": target.id})]
            ),
            _response([_part(text="Иван свободен.")]),
        ]
    )
    result = await agent.ask(
        session=session, ctx=ctx,
        user_text="Что у Ивана?",
        history=None,
    )
    assert "Иван" in result or "свободен" in result


async def test_max_iterations_raises(session):
    """Модель упорно вызывает tools без финального ответа — AiUnavailable."""
    ctx = await _admin_ctx(session)
    # 6 ответов подряд — все function_call'ы; превысит лимит 5 итераций
    agent = _agent_with_responses(
        [
            _response([_part(fc_name="get_user_tasks", fc_args={})])
            for _ in range(6)
        ]
    )
    with pytest.raises(AiUnavailable):
        await agent.ask(
            session=session, ctx=ctx,
            user_text="зацикли меня",
            history=None,
        )


async def test_history_text_only_round_trip(session):
    """История из text-only сообщений не падает при подаче в contents."""
    ctx = await _admin_ctx(session)
    history = [
        {"role": "user", "parts": [{"text": "привет"}]},
        {"role": "model", "parts": [{"text": "Я отвечаю только на вопросы про задачи."}]},
    ]
    agent = _agent_with_responses([_response([_part(text="ОК.")])])
    result = await agent.ask(
        session=session, ctx=ctx,
        user_text="а сейчас по делу: сколько просрочек",
        history=history,
    )
    assert result == "ОК."
