from app.ai.agents.chat_agent import build_chat_agent
from app.ai.config import AISettings
from app.ai.runtime.registry import AgentRegistry
from app.ai.schemas.agent import AgentManifest


def register_default_agents(registry: AgentRegistry, settings: AISettings) -> None:
    """注册当前项目默认启用的 Agent。

    当前只有一个 `chat-agent`，后续如果扩更多 Agent，
    也统一从这里往注册表里挂，避免在 runtime 初始化时散落注册逻辑。
    """
    registry.register(
        manifest=AgentManifest(
            agent_id="chat-agent",
            name="Chat Agent",
            description="General-purpose assistant agent for the FastAPI service.",
            default_model=settings.default_model,
            supports_stream=False,
        ),
        builder=build_chat_agent,
    )
