from __future__ import annotations

import json
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, ValidationError

from app.ai.config import AISettings
from app.ai.exceptions import MCPConfigurationError


class BaseManagedMCPServerConfig(BaseModel):
    """项目内统一使用的 MCP server 配置基类。"""

    id: str = Field(description="MCP server 的稳定标识")
    enabled: bool = Field(default=True, description="是否启用该 server")
    auto_route_enabled: bool = Field(default=True, description="未显式传入 mcp_servers 时是否允许自动路由")
    route_keywords: list[str] = Field(default_factory=list, description="自动路由关键词列表")
    tool_prefix: str | None = Field(default=None, description="工具名前缀，默认回落到 server id")
    timeout: float = Field(default=5.0, description="初始化连接超时时间（秒）")
    read_timeout: float = Field(default=300.0, description="长连接读超时（秒）")
    max_retries: int = Field(default=1, description="MCP 工具调用最大重试次数")
    include_instructions: bool = Field(default=False, description="是否把 MCP instructions 注入给模型")


class ManagedMCPServerStdioConfig(BaseManagedMCPServerConfig):
    """stdio 方式启动的 MCP server 配置。"""

    transport: Literal["stdio"] = "stdio"
    command: str = Field(description="启动命令")
    args: list[str] = Field(default_factory=list, description="命令参数列表")
    env: dict[str, str] | None = Field(default=None, description="子进程环境变量")
    cwd: str | None = Field(default=None, description="子进程工作目录")


class ManagedMCPServerSSEConfig(BaseManagedMCPServerConfig):
    """SSE 方式连接的 MCP server 配置。"""

    transport: Literal["sse"] = "sse"
    url: str = Field(description="SSE MCP endpoint")
    headers: dict[str, str] | None = Field(default=None, description="可选请求头")


class ManagedMCPServerStreamableHTTPConfig(BaseManagedMCPServerConfig):
    """Streamable HTTP 方式连接的 MCP server 配置。"""

    transport: Literal["streamable-http"] = "streamable-http"
    url: str = Field(description="Streamable HTTP MCP endpoint")
    headers: dict[str, str] | None = Field(default=None, description="可选请求头")


ManagedMCPServerConfig = Annotated[
    ManagedMCPServerStdioConfig | ManagedMCPServerSSEConfig | ManagedMCPServerStreamableHTTPConfig,
    Field(discriminator="transport"),
]


class ManagedMCPSettings(BaseModel):
    """MCP 配置载荷。

    环境变量里建议使用：

    ```json
    {
      "mcpServers": {
        "filesystem": {
          "transport": "stdio",
          "command": "uvx",
          "args": ["mcp-server-filesystem", "."]
        }
      }
    }
    ```

    同时也兼容直接传 list 或者直接传 `{id: config}` 的 map 结构，
    方便本地开发和测试。
    """

    mcp_servers: list[ManagedMCPServerConfig] = Field(default_factory=list, alias="mcpServers")


def load_mcp_server_configs(settings: AISettings) -> list[ManagedMCPServerConfig]:
    """从 `AISettings` 提取并解析 MCP server 配置。"""

    if not settings.enable_mcp:
        return []
    return parse_mcp_servers_json(settings.mcp_servers_json)


def parse_mcp_servers_json(raw_json: str | None) -> list[ManagedMCPServerConfig]:
    """解析环境变量中的 MCP server JSON 配置。"""

    if raw_json is None or not raw_json.strip():
        return []

    try:
        payload = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise MCPConfigurationError("AI_MCP_SERVERS_JSON 不是合法的 JSON") from exc

    normalized_payload = _normalize_mcp_payload(payload)
    try:
        settings = ManagedMCPSettings.model_validate(normalized_payload)
    except ValidationError as exc:
        raise MCPConfigurationError("AI_MCP_SERVERS_JSON 不符合 MCP 配置约定") from exc

    return [server for server in settings.mcp_servers if server.enabled]


def _normalize_mcp_payload(payload: Any) -> dict[str, Any]:
    """把多种可接受的输入格式统一规整为 `{"mcpServers": [...]}`。"""

    if isinstance(payload, list):
        return {"mcpServers": [_normalize_mcp_item(item) for item in payload]}

    if not isinstance(payload, dict):
        raise MCPConfigurationError("AI_MCP_SERVERS_JSON 必须是对象或数组")

    if "mcpServers" in payload:
        servers = payload["mcpServers"]
        if isinstance(servers, list):
            return {"mcpServers": [_normalize_mcp_item(item) for item in servers]}
        if isinstance(servers, dict):
            return {
                "mcpServers": [
                    _normalize_mcp_item(item, server_id=server_id)
                    for server_id, item in servers.items()
                ]
            }
        raise MCPConfigurationError("mcpServers 必须是数组或对象")

    return {
        "mcpServers": [
            _normalize_mcp_item(item, server_id=server_id)
            for server_id, item in payload.items()
        ]
    }


def _normalize_mcp_item(item: Any, *, server_id: str | None = None) -> dict[str, Any]:
    """补齐 `id/transport` 等字段，便于后续统一校验。"""

    if not isinstance(item, dict):
        raise MCPConfigurationError("每个 MCP server 配置都必须是对象")

    normalized = dict(item)
    if server_id is not None and "id" not in normalized:
        normalized["id"] = server_id

    if "id" not in normalized or not normalized["id"]:
        raise MCPConfigurationError("每个 MCP server 都必须提供 id")

    if "transport" not in normalized:
        normalized["transport"] = _infer_transport(normalized)

    return normalized


def _infer_transport(item: dict[str, Any]) -> str:
    """在未显式声明 transport 时，根据字段形态推断传输方式。"""

    url = item.get("url")
    if isinstance(url, str) and url:
        return "sse" if url.endswith("/sse") else "streamable-http"
    return "stdio"
