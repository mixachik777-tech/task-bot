"""
FunctionDeclaration-схемы для Gemini (google-genai SDK).

Хранятся отдельно от tools.py:
- tools.py — чистые async-функции, импортируемые/тестируемые без SDK.
- tool_registry.py — описания для модели, тянущие google-genai.

Так юнит-тесты не требуют установленного SDK / валидного API-ключа.
"""

from __future__ import annotations

from google.genai import types


def _str_enum(values: list[str]) -> types.Schema:
    return types.Schema(type=types.Type.STRING, enum=values)


def _str() -> types.Schema:
    return types.Schema(type=types.Type.STRING)


def _int() -> types.Schema:
    return types.Schema(type=types.Type.INTEGER)


PERIOD_ENUM = ["today", "week", "month"]
STATUS_ENUM = ["new", "in_progress", "done", "cancelled"]
DEPT_ENUM = ["correctors", "designers", "correspondents"]


FUNCTION_DECLARATIONS: list[types.FunctionDeclaration] = [
    types.FunctionDeclaration(
        name="get_user_tasks",
        description=(
            "Список задач, где юзер является creator или assignee. "
            "user_id опционален: если не указан — берётся текущий пользователь "
            "(подходит для запросов «мои задачи»). status опционален."
        ),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "user_id": _int(),
                "status": _str_enum(STATUS_ENUM),
            },
        ),
    ),
    types.FunctionDeclaration(
        name="get_user_workload",
        description=(
            "Текущая нагрузка одного юзера за период: сколько принято в работу, "
            "сколько выполнено, сколько сейчас в работе и просрочено, среднее "
            "время выполнения. По умолчанию week."
        ),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "user_id": _int(),
                "period": _str_enum(PERIOD_ENUM),
            },
        ),
    ),
    types.FunctionDeclaration(
        name="get_team_summary",
        description=(
            "Командная сводка за период: общие числа + разбивка по сотрудникам. "
            "Доступно только lead/admin."
        ),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={"period": _str_enum(PERIOD_ENUM)},
        ),
    ),
    types.FunctionDeclaration(
        name="get_overdue_tasks",
        description=(
            "Все текущие просрочки (status new/in_progress + deadline < сейчас). "
            "dept_code опционален: фильтр по отделу-категории."
        ),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={"dept_code": _str_enum(DEPT_ENUM)},
        ),
    ),
    types.FunctionDeclaration(
        name="find_tasks",
        description=(
            "Substring-поиск по тексту в title и description. Возвращает до 20 "
            "задач, отсортированных по дате создания DESC. Опциональные фильтры "
            "status, dept_code, period."
        ),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            required=["query"],
            properties={
                "query": _str(),
                "status": _str_enum(STATUS_ENUM),
                "dept_code": _str_enum(DEPT_ENUM),
                "period": _str_enum(PERIOD_ENUM),
            },
        ),
    ),
    types.FunctionDeclaration(
        name="get_task_details",
        description=(
            "Полная карточка задачи: title, description, статус, отдел, "
            "исполнитель, постановщик, файлы, история событий."
        ),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            required=["task_id"],
            properties={"task_id": _int()},
        ),
    ),
    types.FunctionDeclaration(
        name="get_user_history",
        description=(
            "События задач (created/accepted/completed/cancelled/commented), "
            "инициированные конкретным юзером за период. По умолчанию week."
        ),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "user_id": _int(),
                "period": _str_enum(PERIOD_ENUM),
            },
        ),
    ),
    types.FunctionDeclaration(
        name="find_users",
        description=(
            "Substring-поиск по полному имени и tg_username активных юзеров. "
            "dept_code опционален: фильтр по отделу."
        ),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            required=["query"],
            properties={
                "query": _str(),
                "dept_code": _str_enum(DEPT_ENUM),
            },
        ),
    ),
]


def build_tools_object() -> types.Tool:
    return types.Tool(function_declarations=FUNCTION_DECLARATIONS)
