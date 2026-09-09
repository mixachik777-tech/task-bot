from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    BOT_TOKEN: str = Field(..., min_length=1)
    ADMIN_TG_ID: int
    TASK_CHAT_ID: int = 0
    LEADERSHIP_TOPIC_ID: int = 0

    @field_validator("TASK_CHAT_ID", "LEADERSHIP_TOPIC_ID", mode="before")
    @classmethod
    def _empty_to_zero(cls, v: object) -> object:
        if v in ("", None):
            return 0
        return v

    DB_HOST: str = "postgres"
    DB_PORT: int = 5432
    DB_USER: str = "taskbot"
    DB_PASSWORD: str = Field(..., min_length=1)
    DB_NAME: str = "taskbot"

    REDIS_HOST: str = "redis"
    REDIS_PORT: int = 6379
    REDIS_DB: int = 0

    GEMINI_API_KEY: str = ""
    # Дневной лимит вопросов AI на одного пользователя (через Redis).
    # 0 → лимит отключён (только серверные ограничения Google).
    AI_USER_DAILY_LIMIT: int = 30

    LOG_LEVEL: str = "INFO"
    TZ: str = "Europe/Moscow"

    @property
    def db_dsn(self) -> str:
        return (
            f"postgresql+asyncpg://{self.DB_USER}:{self.DB_PASSWORD}"
            f"@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}"
        )

    @property
    def db_dsn_sync(self) -> str:
        return (
            f"postgresql+psycopg2://{self.DB_USER}:{self.DB_PASSWORD}"
            f"@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}"
        )

    @property
    def redis_url(self) -> str:
        return f"redis://{self.REDIS_HOST}:{self.REDIS_PORT}/{self.REDIS_DB}"


settings = Settings()  # type: ignore[call-arg]
