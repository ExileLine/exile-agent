from dataclasses import dataclass, field
from typing import Literal


ConfigSource = Literal["database", "settings_fallback"]


@dataclass(frozen=True, slots=True)
class ResolvedModelConfig:
    """一次 run 最终选定的模型配置。"""

    model_key: str
    provider_key: str | None
    model_name: str
    supports_stream: bool = True
    supports_tools: bool = True
    supports_json_output: bool = False
    risk_level: str = "low"


@dataclass(frozen=True, slots=True)
class ResolvedProviderConfig:
    """一次 run 最终选定的模型供应商配置。"""

    provider_key: str
    provider_type: str
    base_url: str | None
    api_key_encrypted: str | None
    timeout_seconds: float | None
    max_retries: int | None


@dataclass(frozen=True, slots=True)
class ResolvedMCPServerConfig:
    """一次 run 最终允许装配的 MCP server 配置。"""

    server_key: str
    transport: str
    tool_prefix: str | None
    command: str | None = None
    args: tuple[str, ...] = ()
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    auto_route_enabled: bool = True
    route_keywords: tuple[str, ...] = ()
    timeout_seconds: float | None = None
    read_timeout_seconds: float | None = None
    max_retries: int | None = None
    include_instructions: bool = False
    required_approval: bool = False
    allow_auto_route: bool = True
    risk_level: str = "low"


@dataclass(frozen=True, slots=True)
class ResolvedRunConfig:
    """Runner 后续消费的稳定配置对象。

    resolver 负责把 request、数据库控制面配置和 `.env` fallback 统一折叠成这个结构。
    当前阶段先独立落地，不直接改变 AgentRunner 行为。
    """

    agent_id: str
    model: ResolvedModelConfig
    source: ConfigSource
    provider: ResolvedProviderConfig | None = None
    mcp_servers: tuple[ResolvedMCPServerConfig, ...] = ()
    skill_ids: tuple[str, ...] = ()
    config_version: str | None = None
    runtime_flags: dict[str, bool] = field(default_factory=dict)

    @property
    def model_name(self) -> str:
        return self.model.model_name

    @property
    def model_key(self) -> str:
        return self.model.model_key

    @property
    def mcp_server_keys(self) -> list[str]:
        return [item.server_key for item in self.mcp_servers]
