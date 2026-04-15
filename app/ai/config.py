from pydantic import BaseModel, ConfigDict

from app.core.config import BaseConfig


class AISettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    enabled: bool = True
    default_agent: str = "chat-agent"
    default_model: str = "openai:gpt-5.2"
    max_retries: int = 2
    http_timeout_seconds: float = 30.0
    openai_api_key: str | None = None
    openai_base_url: str | None = None

    @classmethod
    def from_config(cls, config: BaseConfig) -> "AISettings":
        return cls(
            enabled=config.AI_ENABLED,
            default_agent=config.AI_DEFAULT_AGENT,
            default_model=config.AI_DEFAULT_MODEL,
            max_retries=config.AI_MAX_RETRIES,
            http_timeout_seconds=config.AI_HTTP_TIMEOUT_SECONDS,
            openai_api_key=config.OPENAI_API_KEY,
            openai_base_url=config.OPENAI_BASE_URL,
        )
