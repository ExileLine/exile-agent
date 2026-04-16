from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from app.ai.config import AISettings
from app.ai.deps import AgentDeps
from app.ai.toolsets import get_builtin_toolset


def build_chat_agent(settings: AISettings, model_name: str) -> Agent[AgentDeps, str]:
    model = _build_model(settings, model_name)
    agent: Agent[AgentDeps, str] = Agent[AgentDeps, str](
        model=model,
        deps_type=AgentDeps,
        output_type=str,
        name="chat-agent",
        instructions=(
            "You are the default assistant for this FastAPI backend project. "
            "Be concise, factual, and implementation-oriented. "
            "Prefer answering in Chinese unless the user asks otherwise. "
            "When runtime metadata would help, use the available builtin tools instead of guessing."
        ),
        retries=settings.max_retries,
        toolsets=[get_builtin_toolset()],
        defer_model_check=True,
    )

    return agent


def _build_model(settings: AISettings, model_name: str):
    if not model_name.startswith("openai:"):
        return model_name

    if not settings.openai_api_key and not settings.openai_base_url:
        return model_name

    provider = OpenAIProvider(
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url,
    )
    return OpenAIChatModel(model_name=model_name.removeprefix("openai:"), provider=provider)
