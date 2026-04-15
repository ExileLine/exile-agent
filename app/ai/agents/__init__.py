from app.ai.agents.chat_agent import build_chat_agent
from app.ai.config import AISettings
from app.ai.runtime.registry import AgentRegistry
from app.ai.schemas.agent import AgentManifest


def register_default_agents(registry: AgentRegistry, settings: AISettings) -> None:
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
