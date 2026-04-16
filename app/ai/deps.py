from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.config import AISettings

if TYPE_CHECKING:
    from app.ai.services.tool_audit import ToolAuditService


@dataclass(slots=True)
class RequestContext:
    request_id: str
    user_id: str | None = None
    session_id: str | None = None


@dataclass(slots=True)
class AgentDeps:
    request: RequestContext
    settings: AISettings
    db_session_factory: async_sessionmaker[AsyncSession] | None
    redis: Redis | None
    http_client: httpx.AsyncClient
    tool_audit: ToolAuditService
