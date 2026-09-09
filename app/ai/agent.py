"""
GeminiAgent — обёртка над google-genai с tool-loop'ом.

Контракт:
- ask(session, ctx, user_text, history) → str ответа.
- tool_calls и tool_responses происходят ВНУТРИ одного ask, в БД-историю
  записываются только финальные user_text + model_text. Это упрощает
  rehydration: contents для следующего запроса — простая последовательность
  text-only turns.
- На ошибки: 429 → AiRateLimit, тайм-аут → AiTimeout, остальное → AiUnavailable.
"""

from __future__ import annotations

import asyncio
from typing import Any

from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from loguru import logger

from app.ai.context import UserContext
from app.ai.errors import AiRateLimit, AiTimeout, AiUnavailable
from app.ai.prompts import build_system_prompt
from app.ai.tool_registry import build_tools_object
from app.ai.tools import TOOL_HANDLERS
from app.config import settings

MODEL = "gemini-2.5-flash"
MAX_TOOL_ITERATIONS = 5
GENERATE_TIMEOUT_S = 30


def _content_history_for_text(history: list[dict[str, Any]] | None) -> list[dict]:
    """
    Из БД-истории (только text turns) собирает contents для подачи в Gemini.
    Невалидные элементы пропускаются — лучше потерять часть истории,
    чем уронить вызов.
    """
    if not history:
        return []
    out: list[dict] = []
    for h in history:
        role = h.get("role")
        parts = h.get("parts") or []
        if role not in ("user", "model"):
            continue
        clean_parts = [
            {"text": p["text"]} for p in parts if isinstance(p, dict) and "text" in p
        ]
        if clean_parts:
            out.append({"role": role, "parts": clean_parts})
    return out


class GeminiAgent:
    def __init__(self, api_key: str | None = None) -> None:
        key = api_key or settings.GEMINI_API_KEY
        if not key:
            raise RuntimeError("GEMINI_API_KEY не задан")
        self._client = genai.Client(api_key=key)
        self._tools_obj = build_tools_object()

    async def ask(
        self,
        *,
        session,
        ctx: UserContext,
        user_text: str,
        history: list[dict[str, Any]] | None,
    ) -> str:
        contents = _content_history_for_text(history)
        contents.append({"role": "user", "parts": [{"text": user_text}]})

        config = types.GenerateContentConfig(
            system_instruction=build_system_prompt(ctx),
            tools=[self._tools_obj],
            temperature=0.2,
            max_output_tokens=2048,
        )

        for iteration in range(MAX_TOOL_ITERATIONS):
            response = await self._generate(contents, config)
            candidate = response.candidates[0] if response.candidates else None
            if candidate is None or candidate.content is None:
                raise AiUnavailable("empty response from model")
            parts = candidate.content.parts or []
            tool_calls = [p.function_call for p in parts if p.function_call is not None]

            # добавляем model-turn в локальный rolling contents для следующего
            # витка tool-loop'а (в БД эту запись НЕ сохраняем)
            model_parts: list[dict] = []
            for p in parts:
                if p.function_call is not None:
                    model_parts.append(
                        {
                            "function_call": {
                                "name": p.function_call.name,
                                "args": dict(p.function_call.args or {}),
                            }
                        }
                    )
                elif p.text:
                    model_parts.append({"text": p.text})
            contents.append({"role": "model", "parts": model_parts})

            if not tool_calls:
                final = "\n".join(p.text for p in parts if p.text).strip()
                return final or "Не понял запрос, попробуй переформулировать."

            response_parts: list[dict] = []
            for fc in tool_calls:
                payload = await self._exec_tool(session, ctx, fc)
                response_parts.append(
                    {
                        "function_response": {
                            "name": fc.name,
                            "response": payload,
                        }
                    }
                )
            contents.append({"role": "user", "parts": response_parts})

        raise AiUnavailable("превышено число итераций tool-loop")

    async def _generate(self, contents: list[dict], config: types.GenerateContentConfig):
        try:
            return await asyncio.wait_for(
                self._client.aio.models.generate_content(
                    model=MODEL,
                    contents=contents,
                    config=config,
                ),
                timeout=GENERATE_TIMEOUT_S,
            )
        except asyncio.TimeoutError as exc:
            raise AiTimeout() from exc
        except genai_errors.ClientError as exc:
            code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
            if code == 429:
                raise AiRateLimit() from exc
            logger.warning("gemini ClientError code={} msg={}", code, exc)
            raise AiUnavailable() from exc
        except genai_errors.ServerError as exc:
            logger.warning("gemini ServerError: {}", exc)
            raise AiUnavailable() from exc
        except Exception as exc:  # noqa: BLE001
            logger.exception("gemini unexpected error: {}", exc)
            raise AiUnavailable() from exc

    async def _exec_tool(self, session, ctx: UserContext, fc) -> dict:
        handler = TOOL_HANDLERS.get(fc.name)
        args = dict(fc.args or {})
        if handler is None:
            logger.warning("ai: unknown tool '{}' requested", fc.name)
            return {"error": f"unknown tool: {fc.name}"}
        try:
            payload = await handler(session, ctx, **args)
            return payload
        except TypeError as exc:
            # неверные аргументы от модели — отдаём как tool-error, чтобы
            # модель могла переформулировать
            logger.warning("ai: tool {} bad args {}: {}", fc.name, args, exc)
            return {"error": f"bad arguments: {exc}"}
        except Exception:  # noqa: BLE001
            logger.exception("ai: tool {} failed", fc.name)
            return {"error": "ошибка инструмента"}
