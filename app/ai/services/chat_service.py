from app.ai.deps import RequestContext
from app.ai.runtime.manager import AgentManager
from app.ai.runtime.runner import AgentRunner
from app.ai.schemas.agent import AgentManifest
from app.ai.schemas.chat import AgentChatRequest, AgentChatResponse


class ChatService:
    def __init__(self, *, runner: AgentRunner, agent_manager: AgentManager) -> None:
        self.runner = runner
        self.agent_manager = agent_manager

    def list_agents(self) -> list[AgentManifest]:
        return self.agent_manager.list_agents()

    async def chat(self, *, request_context: RequestContext, payload: AgentChatRequest) -> AgentChatResponse:
        return await self.runner.run_chat(
            request_context=request_context,
            message=payload.message,
            agent_id=payload.agent_id,
            session_id=payload.session_id,
            model_name=payload.model,
        )
