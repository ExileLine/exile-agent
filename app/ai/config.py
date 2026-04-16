from pydantic import BaseModel, ConfigDict

from app.core.config import BaseConfig


class AISettings(BaseModel):
    """AI 子系统自己的配置视图。

    项目总配置里可能还有很多与 AI 无关的字段，
    这里单独抽一层 `AISettings` 的目的，是让 runtime / agent / runner
    这些模块只依赖自己真正关心的配置，避免一路透传整个 `BaseConfig`。
    """
    model_config = ConfigDict(extra="ignore")

    enabled: bool = True
    default_agent: str = "chat-agent"
    default_model: str = "openai:gpt-5.2"
    max_retries: int = 2
    http_timeout_seconds: float = 30.0
    history_ttl_seconds: int | None = None
    openai_api_key: str | None = None
    openai_base_url: str | None = None

    @classmethod
    def from_config(cls, config: BaseConfig) -> "AISettings":
        """从全局配置对象提取 AI 运行时需要的最小配置集。"""
        return cls(
            enabled=config.AI_ENABLED,
            default_agent=config.AI_DEFAULT_AGENT,
            default_model=config.AI_DEFAULT_MODEL,
            max_retries=config.AI_MAX_RETRIES,
            http_timeout_seconds=config.AI_HTTP_TIMEOUT_SECONDS,
            history_ttl_seconds=config.AI_HISTORY_TTL_SECONDS,
            openai_api_key=config.OPENAI_API_KEY,
            openai_base_url=config.OPENAI_BASE_URL,
        )
