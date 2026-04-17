from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.config import AISettings

if TYPE_CHECKING:
    from app.ai.mcp import MCPManager
    from app.ai.services.tool_audit import ToolAuditService


@dataclass(slots=True)
class RequestContext:
    """当前请求在 AI 层关心的最小上下文。

    这里故意不直接把 `FastAPI Request` 往下传，而是只保留对工具层和运行层真正有价值的字段。
    这样可以让 AI 层保持和 Web 框架解耦。
    """
    request_id: str
    user_id: str | None = None
    session_id: str | None = None


@dataclass(slots=True)
class AgentDeps:
    """一次 Agent 运行注入给工具层 / 动态 instructions 的依赖集合。

    `deps_type=AgentDeps` 后，工具函数里的 `RunContext[AgentDeps]` 就可以统一拿到这些资源。
    当前先注入 request/settings/db/redis/http_client/tool_audit/mcp_manager，
    后续接 history / skill / approval 也会继续沿这个入口扩展。
    """
    request: RequestContext
    settings: AISettings
    db_session_factory: async_sessionmaker[AsyncSession] | None
    redis: Redis | None
    http_client: httpx.AsyncClient
    tool_audit: ToolAuditService
    mcp_manager: MCPManager | None
