"""Кастомные исключения AI-агента — для маппинга на user-facing сообщения."""


class AiError(Exception):
    """Базовое исключение AI-агента."""


class AiUnavailable(AiError):
    """Модель/SDK сбоит: 5xx, network, неизвестная ошибка."""


class AiRateLimit(AiError):
    """HTTP 429 от Gemini — лимит запросов."""


class AiTimeout(AiError):
    """Запрос превысил GENERATE_TIMEOUT_S."""
