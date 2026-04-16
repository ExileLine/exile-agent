from collections.abc import AsyncIterator

from app.ai.deps import RequestContext
from app.ai.runtime.manager import AgentManager
from app.ai.runtime.runner import AgentRunner
from app.ai.schemas.agent import AgentManifest
from app.ai.schemas.chat import AgentChatRequest, AgentChatResponse, AgentChatResumeRequest


class ChatService:
    """面向 endpoint 的轻量服务层。

    endpoint 不直接碰 runner / manager 的细节，而是通过 service 暴露：
    - `list_agents()`
    - `chat(...)`

    这样 Web 层和 AI 运行层之间会有一层更稳定的边界。
    """
    def __init__(self, *, runner: AgentRunner, agent_manager: AgentManager) -> None:
        self.runner = runner
        self.agent_manager = agent_manager

    def list_agents(self) -> list[AgentManifest]:
        return self.agent_manager.list_agents()

    async def chat(self, *, request_context: RequestContext, payload: AgentChatRequest) -> AgentChatResponse:
        """把 endpoint 请求转换成一次标准的 runner chat 调用。"""
        return await self.runner.run_chat(
            request_context=request_context,
            message=payload.message,
            agent_id=payload.agent_id,
            session_id=payload.session_id,
            model_name=payload.model,
        )

    async def stream(self, *, request_context: RequestContext, payload: AgentChatRequest) -> AsyncIterator[str]:
        """把 endpoint 请求转换成一次标准的 runner stream 调用。"""

        async for event in self.runner.run_chat_stream(
            request_context=request_context,
            message=payload.message,
            agent_id=payload.agent_id,
            session_id=payload.session_id,
            model_name=payload.model,
        ):
            yield event

    async def resume(self, *, request_context: RequestContext, payload: AgentChatResumeRequest) -> AgentChatResponse:
        """继续执行上一轮因 approval 停住的 run。"""

        return await self.runner.resume_chat(
            request_context=request_context,
            message_history_json=payload.message_history_json,
            approvals=payload.approvals,
            agent_id=payload.agent_id,
            session_id=payload.session_id,
            model_name=payload.model,
        )
