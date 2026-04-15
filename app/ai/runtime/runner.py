from typing import Any

import httpx
import shortuuid

from app.ai.config import AISettings
from app.ai.deps import AgentDeps, RequestContext
from app.ai.exceptions import AIDisabledError
from app.ai.runtime.manager import AgentManager
from app.ai.schemas.chat import AgentChatResponse
from app.db import redis_client
from app.db.session import AsyncSessionLocal


class AgentRunner:
    def __init__(
        self,
        *,
        settings: AISettings,
        agent_manager: AgentManager,
        http_client: httpx.AsyncClient,
    ) -> None:
        self.settings = settings
        self.agent_manager = agent_manager
        self.http_client = http_client

    async def run_chat(
        self,
        *,
        request_context: RequestContext,
        message: str,
        agent_id: str | None = None,
        session_id: str | None = None,
        model_name: str | None = None,
    ) -> AgentChatResponse:
        if not self.settings.enabled:
            raise AIDisabledError("AI 能力已关闭")

        resolved_agent_id = agent_id or self.settings.default_agent
        resolved_model = self.agent_manager.resolve_model(resolved_agent_id, model_name)
        agent = self.agent_manager.get_agent(resolved_agent_id, resolved_model)
        deps = AgentDeps(
            request=request_context,
            settings=self.settings,
            db_session_factory=AsyncSessionLocal,
            redis=redis_client.redis_pool,
            http_client=self.http_client,
        )
        result = await agent.run(message, deps=deps)

        return AgentChatResponse(
            run_id=shortuuid.uuid(),
            agent_id=resolved_agent_id,
            model=resolved_model,
            message=result.output,
            request_id=request_context.request_id,
            session_id=session_id,
            usage=self._serialize_usage(result),
        )

    @staticmethod
    def _serialize_usage(result: Any) -> dict[str, Any] | None:
        usage_value = getattr(result, "usage", None)
        usage = usage_value() if callable(usage_value) else usage_value
        if usage is None:
            return None
        if hasattr(usage, "model_dump"):
            return usage.model_dump(mode="json")
        if hasattr(usage, "__dict__"):
            return dict(vars(usage))
        return {"value": str(usage)}
