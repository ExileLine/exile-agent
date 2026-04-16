import datetime as dt

from pydantic_ai import RunContext
from pydantic_ai.toolsets.function import FunctionToolset

from app.ai.deps import AgentDeps


def get_builtin_toolset() -> FunctionToolset[AgentDeps]:
    toolset = FunctionToolset[AgentDeps](
        id="builtin-toolset",
        instructions=(
            "Use builtin tools when the user asks for request metadata, current time, "
            "or a concise summary of the current AI runtime configuration."
        ),
    )

    @toolset.tool_plain
    def get_current_utc_time() -> str:
        """Return the current UTC time in ISO 8601 format."""
        return dt.datetime.now(dt.UTC).isoformat()

    @toolset.tool
    def get_request_context(ctx: RunContext[AgentDeps]) -> dict[str, str | None]:
        """Return request metadata for the current run."""
        request = ctx.deps.request
        return {
            "request_id": request.request_id,
            "user_id": request.user_id,
            "session_id": request.session_id,
        }

    @toolset.tool
    def get_runtime_config_summary(ctx: RunContext[AgentDeps]) -> dict[str, object]:
        """Return a readonly summary of the current AI runtime configuration."""
        settings = ctx.deps.settings
        return {
            "enabled": settings.enabled,
            "default_agent": settings.default_agent,
            "default_model": settings.default_model,
            "max_retries": settings.max_retries,
            "http_timeout_seconds": settings.http_timeout_seconds,
            "has_openai_api_key": bool(settings.openai_api_key),
            "has_openai_base_url": bool(settings.openai_base_url),
        }

    @toolset.tool
    async def check_runtime_resources(ctx: RunContext[AgentDeps]) -> dict[str, bool]:
        """Check whether core runtime resources are available for the current run."""
        return {
            "has_request_context": ctx.deps.request is not None,
            "has_db_session_factory": ctx.deps.db_session_factory is not None,
            "has_redis_pool": ctx.deps.redis is not None,
            "has_http_client": ctx.deps.http_client is not None,
        }

    return toolset
