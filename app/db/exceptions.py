"""Доменные исключения уровня репозиториев / БД."""


class DBError(Exception):
    """Базовый класс для ошибок слоя app.db."""


class InvalidStatusTransition(DBError):
    """
    Поднимается при попытке выполнить переход статуса задачи, который
    не разрешён ALLOWED_STATUS_TRANSITIONS (см. app.db.enums).
    """

    def __init__(self, from_status: str, to_status: str) -> None:
        self.from_status = from_status
        self.to_status = to_status
        super().__init__(f"Invalid status transition: {from_status} -> {to_status}")


class TaskPostingFailed(Exception):
    """
    Задача в БД создана, но публикация карточки в Telegram упала (BadRequest,
    Forbidden, retry-exhausted). Хэндлер при ловле этого исключения должен
    сказать постановщику, что задача создана, но не отправилась в чат —
    задача видна в архиве по `task_id`.
    """

    def __init__(self, task_id: int, reason: str) -> None:
        self.task_id = task_id
        self.reason = reason
        super().__init__(f"Task #{task_id} created but posting failed: {reason}")
