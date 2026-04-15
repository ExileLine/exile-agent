from dataclasses import dataclass

import httpx
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.config import AISettings


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
