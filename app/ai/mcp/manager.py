from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

import httpx
from loguru import logger
from pydantic_ai.toolsets.abstract import AbstractToolset

from app.ai.deps import AgentDeps
from app.ai.exceptions import MCPConfigurationError, MCPServerNotFoundError
from app.ai.mcp.config import (
    ManagedMCPServerConfig,
    ManagedMCPServerSSEConfig,
    ManagedMCPServerStdioConfig,
    ManagedMCPServerStreamableHTTPConfig,
)
from app.ai.toolsets import wrap_toolsets_with_audit, wrap_toolsets_with_metadata_approval

if TYPE_CHECKING:
    from pydantic_ai.mcp import MCPServerSSE, MCPServerStdio, MCPServerStreamableHTTP

    ManagedMCPServer = MCPServerStdio | MCPServerSSE | MCPServerStreamableHTTP
else:
    ManagedMCPServer = Any


class MCPManager:
    """统一管理 MCP server 配置、实例缓存和请求级 toolset 装配。"""

    def __init__(
        self,
        *,
        enabled: bool,
        server_configs: Sequence[ManagedMCPServerConfig],
        http_client: httpx.AsyncClient,
    ) -> None:
        self.enabled = enabled
        self.http_client = http_client
        self._config_by_id = {config.id: config for config in server_configs if config.enabled}
        self._server_cache: dict[str, ManagedMCPServer] = {}

    def list_servers(self) -> list[ManagedMCPServerConfig]:
        """列出当前注册的 MCP server 配置。"""

        return list(self._config_by_id.values())

    def resolve_server_ids(
        self,
        *,
        requested_server_ids: Sequence[str] | None,
        message: str | None = None,
    ) -> list[str]:
        """解析本轮实际应启用的 MCP server ID。

        规则：
        - 请求显式传了 `mcp_servers`：直接使用显式值
        - 请求未显式传：尝试按消息内容做自动路由
        """

        explicit_server_ids = _dedupe_server_ids(requested_server_ids or [])
        if explicit_server_ids:
            return explicit_server_ids
        if not self.enabled or not message:
            return []
        return self._auto_route_server_ids(message)

    def build_toolsets(self, server_ids: Sequence[str] | None) -> list[AbstractToolset[AgentDeps]]:
        """把请求中的 MCP server ID 列表转换为本轮附加 toolsets。"""

        if not server_ids:
            return []
        if not self.enabled:
            raise MCPConfigurationError("MCP 能力未启用，请先打开 AI_ENABLE_MCP")

        servers = [self._get_or_create_server(server_id) for server_id in _dedupe_server_ids(server_ids)]
        # MCP server 本质上也是 toolset。
        # 这里统一复用现有 approval + audit 包装，保证治理策略与内建工具一致。
        return wrap_toolsets_with_audit(wrap_toolsets_with_metadata_approval(servers))

    async def shutdown(self) -> None:
        """关闭已缓存的 MCP server 连接。"""

        for server_id, server in list(self._server_cache.items()):
            while server.is_running:
                try:
                    await server.__aexit__(None, None, None)
                except Exception:
                    logger.exception("关闭 MCP server 失败: {}", server_id)
                    break
        self._server_cache.clear()

    def _get_or_create_server(self, server_id: str) -> ManagedMCPServer:
        if server_id not in self._server_cache:
            config = self._config_by_id.get(server_id)
            if config is None:
                raise MCPServerNotFoundError(f"未找到 MCP server: {server_id}")
            self._server_cache[server_id] = self._build_server(config)
        return self._server_cache[server_id]

    def _build_server(self, config: ManagedMCPServerConfig) -> ManagedMCPServer:
        try:
            from pydantic_ai.mcp import MCPServerSSE, MCPServerStdio, MCPServerStreamableHTTP
        except ImportError as exc:
            raise MCPConfigurationError(
                "当前环境未安装 MCP 依赖，请安装 `mcp` 或 `pydantic-ai-slim[mcp]` 后再启用 MCP server"
            ) from exc

        if isinstance(config, ManagedMCPServerStdioConfig):
            return MCPServerStdio(
                command=config.command,
                args=config.args,
                env=config.env,
                cwd=config.cwd,
                id=config.id,
                tool_prefix=config.tool_prefix or config.id,
                timeout=config.timeout,
                read_timeout=config.read_timeout,
                max_retries=config.max_retries,
                include_instructions=config.include_instructions,
            )

        http_client = None if config.headers else self.http_client
        if isinstance(config, ManagedMCPServerSSEConfig):
            return MCPServerSSE(
                url=config.url,
                headers=config.headers,
                http_client=http_client,
                id=config.id,
                tool_prefix=config.tool_prefix or config.id,
                timeout=config.timeout,
                read_timeout=config.read_timeout,
                max_retries=config.max_retries,
                include_instructions=config.include_instructions,
            )

        return MCPServerStreamableHTTP(
            url=config.url,
            headers=config.headers,
            http_client=http_client,
            id=config.id,
            tool_prefix=config.tool_prefix or config.id,
            timeout=config.timeout,
            read_timeout=config.read_timeout,
            max_retries=config.max_retries,
            include_instructions=config.include_instructions,
        )

    def _auto_route_server_ids(self, message: str) -> list[str]:
        """根据消息内容从已注册 MCP server 中挑选命中的 server。"""

        normalized_message = _normalize_text(message)
        if not normalized_message:
            return []

        matched_server_ids: list[str] = []
        for config in self._config_by_id.values():
            if not config.auto_route_enabled:
                continue
            candidate_keywords = _build_route_keywords(config)
            if any(keyword in normalized_message for keyword in candidate_keywords):
                matched_server_ids.append(config.id)
        return matched_server_ids


def _dedupe_server_ids(server_ids: Sequence[str]) -> list[str]:
    """按出现顺序去重，避免同一个 MCP server 在一轮 run 里重复装配。"""

    deduped: list[str] = []
    seen: set[str] = set()
    for server_id in server_ids:
        if server_id in seen:
            continue
        seen.add(server_id)
        deduped.append(server_id)
    return deduped


def _normalize_text(value: str) -> str:
    """统一把路由匹配文本规整成更稳定的小写字符串。"""

    return " ".join(value.casefold().split())


def _build_route_keywords(config: ManagedMCPServerConfig) -> list[str]:
    """构造单个 MCP server 的自动路由关键词集合。

    显式配置的 `route_keywords` 优先级最高；
    同时默认补上 `id` 和 `tool_prefix`，方便最小配置场景直接生效。
    """

    keywords: list[str] = []
    seen: set[str] = set()

    for raw_keyword in [*config.route_keywords, config.id, config.tool_prefix or ""]:
        normalized_keyword = _normalize_text(raw_keyword)
        if not normalized_keyword or normalized_keyword in seen:
            continue
        seen.add(normalized_keyword)
        keywords.append(normalized_keyword)

    return keywords
