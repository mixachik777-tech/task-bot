"""
FSM-состояния для бота. Состояния хранятся в RedisStorage → переживают
рестарт процесса.

CreateTask — мастер создания задачи (Этап 4).
TaskComplete — ввод комментария о результате при «✅ Завершить» (Этап 5).
TaskComment  — ввод текста для «💬 Комментарий» к задаче (Этап 5).
"""

from aiogram.fsm.state import State, StatesGroup


class CreateTask(StatesGroup):
    department = State()
    title = State()
    priority = State()
    deadline = State()
    description = State()
    # Подмастер для отдела «Дизайнеры»: вместо одного `description`
    # вводятся 3 структурированных поля. Итоговый текст склеивается
    # в многострочный `description` и пишется в ту же колонку `tasks.description`.
    design_purpose = State()
    design_format = State()
    design_reference = State()
    files = State()
    preview = State()


class TaskComplete(StatesGroup):
    # FSM data: {
    #   "task_id": int,
    #   "started_at": float (time.monotonic),
    #   "comment": str | None,
    #   "files": list[{"kind": "document"|"photo"|"video"|"animation",
    #                  "file_id": str, "file_name": str | None}],
    # }
    # Юзер шлёт текст / документ / фото / видео по одному или нескольким
    # сообщениям; всё копится в state. По «✅ Готово» сервис собирает
    # отчёт и отправляет в личку постановщику.
    waiting_comment = State()


class TaskComment(StatesGroup):
    waiting_text = State()  # FSM data: {"task_id": int}


class TaskQuestion(StatesGroup):
    # FSM data: {
    #   "task_id": int,
    #   "started_at": float,
    #   "text": str | None,
    #   "file": {"kind", "file_id", "file_name"} | None,
    # }
    # Этап Г. Исполнитель нажал «❓ Задать вопрос» в карточке задачи →
    # бот ждёт текст вопроса (опционально + один файл). По «✉️ Отправить»
    # вопрос летит постановщику в DM с inline-кнопкой «✏️ Ответить».
    waiting_text = State()


class TaskQuestionAnswer(StatesGroup):
    # FSM data: {
    #   "task_id": int,
    #   "question_history_id": int,
    #   "assignee_tg_id": int,
    #   "started_at": float,
    #   "text": str | None,
    #   "file": {"kind", "file_id", "file_name"} | None,
    # }
    # Этап Г, ответная ветка. Постановщик нажал «✏️ Ответить» на вопрос
    # исполнителя → бот ждёт текст ответа (опционально + один файл).
    # По «✉️ Отправить» ответ летит исполнителю в DM.
    waiting_text = State()


class RequestRework(StatesGroup):
    # FSM data: {
    #   "task_id": int,
    #   "started_at": float,
    #   "comment": str | None,   # ОБЯЗАТЕЛЕН для отправки
    #   "files": list[{"kind", "file_id", "file_name"}],
    # }
    # Этап В. Постановщик нажал «✏️ На доработку» → бот просит текст
    # с правками (обязательно) и опциональные файлы-референсы.
    # По «✅ Отправить на доработку» вызывается TaskActionsService.request_rework
    # с comment+files, исполнителю в личку уходит сводка.
    waiting_comment = State()
