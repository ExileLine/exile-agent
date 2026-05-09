from collections.abc import AsyncIterator

from app.ai.deps import RequestContext
from app.ai.runtime.manager import AgentManager
from app.ai.runtime.runner import AgentRunner
from app.ai.skills import SkillRegistry
from app.ai.schemas.agent import AgentManifest
from app.ai.schemas.chat import AgentChatRequest, AgentChatResponse, AgentChatResumeRequest


class ChatService:
    """面向 endpoint 的轻量服务层。

    endpoint 不直接碰 runner / manager 的细节，而是通过 service 暴露：
    - `list_agents()`
    - `chat(...)`

    这样 Web 层和 AI 运行层之间会有一层更稳定的边界。
    """
    def __init__(
        self,
        *,
        runner: AgentRunner,
        agent_manager: AgentManager,
        skill_registry: SkillRegistry | None = None,
    ) -> None:
        self.runner = runner
        self.agent_manager = agent_manager
        self.skill_registry = skill_registry

    def list_agents(self) -> list[AgentManifest]:
        return self.agent_manager.list_agents()

    def list_skills(self) -> list[dict]:
        if self.skill_registry is None:
            return []
        return [skill.model_dump(mode="json") for skill in self.skill_registry.list_skills()]

    async def get_team_run_trace(self, team_run_id: str) -> dict | None:
        return await self.runner.get_team_run_trace(team_run_id)

    async def get_session_histories(
        self,
        *,
        request_context: RequestContext,
        session_id: str,
        agent_ids: list[str] | None = None,
        merge: bool = False,
    ) -> dict:
        del merge
        resolved_agent_ids = _normalize_history_agent_ids(agent_ids) if agent_ids else None
        return await self.runner.history_store.load_session_history(
            session_id=session_id,
            user_id=request_context.user_id,
            tenant_id=request_context.tenant_id,
            agent_ids=resolved_agent_ids,
        )

    async def get_user_session_histories(
        self,
        *,
        request_context: RequestContext,
        user_id: str,
        page: int,
        size: int,
        agent_ids: list[str] | None = None,
    ) -> dict:
        resolved_agent_ids = _normalize_history_agent_ids(agent_ids) if agent_ids else None
        records, total = await self.runner.history_store.paginate_user_sessions(
            user_id=user_id,
            tenant_id=request_context.tenant_id,
            agent_ids=resolved_agent_ids,
            page=page,
            size=size,
        )

        return {
            "records": records,
            "now_page": page,
            "total": total,
        }

    async def chat(self, *, request_context: RequestContext, payload: AgentChatRequest) -> AgentChatResponse:
        """把 endpoint 请求转换成一次标准的 runner chat 调用。"""
        return await self.runner.run_chat(
            request_context=request_context,
            message=payload.message,
            agent_id=payload.agent_id,
            session_id=payload.session_id,
            model_name=payload.model,
            mcp_server_ids=payload.mcp_servers,
            skill_ids=payload.skill_ids,
            skill_tags=payload.skill_tags,
        )

    async def stream(self, *, request_context: RequestContext, payload: AgentChatRequest) -> AsyncIterator[str]:
        """把 endpoint 请求转换成一次标准的 runner stream 调用。"""

        async for event in self.runner.run_chat_stream(
            request_context=request_context,
            message=payload.message,
            agent_id=payload.agent_id,
            session_id=payload.session_id,
            model_name=payload.model,
            mcp_server_ids=payload.mcp_servers,
            skill_ids=payload.skill_ids,
            skill_tags=payload.skill_tags,
        ):
            yield event

    async def resume(self, *, request_context: RequestContext, payload: AgentChatResumeRequest) -> AgentChatResponse:
        """继续执行上一轮因 approval 停住的 run。"""

        return await self.runner.resume_chat(
            request_context=request_context,
            message_history_json=payload.message_history_json,
            approvals=payload.approvals,
            approval_id=payload.approval_id,
            agent_id=payload.agent_id,
            session_id=payload.session_id,
            model_name=payload.model,
            mcp_server_ids=payload.mcp_servers,
            skill_ids=payload.skill_ids,
            skill_tags=payload.skill_tags,
        )


def _normalize_history_agent_ids(agent_ids: list[str] | None) -> list[str]:
    source = agent_ids or ["chat-agent", "team:auto-parallel"]
    normalized: list[str] = []
    seen: set[str] = set()
    for agent_id in source:
        item = agent_id.strip()
        if not item or item in seen:
            continue
        seen.add(item)
        normalized.append(item)
    return normalized
