from app.ai.deps import RequestContext
from app.ai.runtime.manager import AgentManager
from app.ai.runtime.runner import AgentRunner
from app.ai.schemas.agent import AgentManifest
from app.ai.schemas.chat import AgentChatRequest, AgentChatResponse


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
