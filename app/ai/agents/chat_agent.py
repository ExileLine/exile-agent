import datetime as dt

from pydantic_ai import Agent, RunContext
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from app.ai.config import AISettings
from app.ai.deps import AgentDeps


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
            "Prefer answering in Chinese unless the user asks otherwise."
        ),
        retries=settings.max_retries,
        defer_model_check=True,
    )

    @agent.tool_plain
    def get_current_utc_time() -> str:
        return dt.datetime.now(dt.UTC).isoformat()

    @agent.tool
    def get_request_context(ctx: RunContext[AgentDeps]) -> dict[str, str | None]:
        request = ctx.deps.request
        return {
            "request_id": request.request_id,
            "user_id": request.user_id,
            "session_id": request.session_id,
        }

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
