import datetime as dt

from pydantic_ai import RunContext
from pydantic_ai.toolsets.function import FunctionToolset

from app.ai.deps import AgentDeps
from app.ai.toolsets.conventions import create_function_toolset, validate_toolset_conventions
from app.ai.toolsets.metadata import build_tool_metadata, build_toolset_metadata

# builtin 工具当前统一归平台层维护。
# 后续如果不同域的 builtin 工具由不同模块维护，可以再按需拆 owner。
BUILTIN_TOOLSET_OWNER = "platform"

# 这里不再使用单个 `builtin-toolset`，而是按能力域拆成多个稳定 id。
# 这样后续继续扩展 builtin 工具时，不会把所有能力都堆进一个大 toolset 里。
BUILTIN_TIME_TOOLSET_ID = "builtin-time-toolset"
BUILTIN_REQUEST_TOOLSET_ID = "builtin-request-toolset"
BUILTIN_RUNTIME_TOOLSET_ID = "builtin-runtime-toolset"


def _build_builtin_toolset(
    *,
    toolset_id: str,
    instructions: str,
) -> FunctionToolset[AgentDeps]:
    """构造一个 builtin toolset 的公共骨架。

    每个 builtin toolset 都共享同一套默认属性：
    - kind 固定为 `builtin`
    - owner 固定为 `platform`
    - 默认是只读、低风险、无需审批

    真正的差异化能力，由外层各个 `get_builtin_xxx_toolset()` 再继续往里注册具体工具。
    """
    return create_function_toolset(
        id=toolset_id,
        metadata=build_toolset_metadata(
            toolset_id=toolset_id,
            kind="builtin",
            owner=BUILTIN_TOOLSET_OWNER,
            readonly=True,
            risk="low",
            approval_required=False,
            tags=["builtin", "system", "readonly"],
        ),
        instructions=instructions,
    )


def get_builtin_time_toolset() -> FunctionToolset[AgentDeps]:
    """时间相关的 builtin 只读工具。"""
    toolset: FunctionToolset[AgentDeps] = _build_builtin_toolset(
        toolset_id=BUILTIN_TIME_TOOLSET_ID,
        instructions="Use these builtin tools when the user asks for the current time.",
    )

    # 这是一个纯函数工具，不依赖 `ctx.deps`，所以使用 `tool_plain`。
    @toolset.tool_plain(
        metadata=build_tool_metadata(
            category="time",
            readonly=True,
            risk="low",
            approval_required=False,
            tags=["builtin", "system", "readonly", "time", "utility"],
        ),
    )
    def get_current_utc_time() -> str:
        """Return the current UTC time in ISO 8601 format."""
        return dt.datetime.now(dt.UTC).isoformat()

    # toolset 构建完成后，立刻执行本地规范校验。
    # 这样如果后面有人往这里新增工具但不符合命名/描述/schema 规范，会尽早失败。
    validate_toolset_conventions(toolset)
    return toolset


def get_builtin_request_toolset() -> FunctionToolset[AgentDeps]:
    """请求上下文相关的 builtin 只读工具。"""
    toolset: FunctionToolset[AgentDeps] = _build_builtin_toolset(
        toolset_id=BUILTIN_REQUEST_TOOLSET_ID,
        instructions="Use these builtin tools when the user asks for request metadata.",
    )

    # 这里依赖当前请求的运行时上下文，因此要使用 `tool` 而不是 `tool_plain`。
    @toolset.tool(
        metadata=build_tool_metadata(
            category="request",
            readonly=True,
            risk="low",
            approval_required=False,
            tags=["builtin", "system", "readonly", "request", "debug"],
        ),
    )
    def get_request_context(ctx: RunContext[AgentDeps]) -> dict[str, str | None]:
        """Return request metadata for the current run."""
        request = ctx.deps.request
        return {
            "request_id": request.request_id,
            "user_id": request.user_id,
            "session_id": request.session_id,
        }

    validate_toolset_conventions(toolset)
    return toolset


def get_builtin_runtime_toolset() -> FunctionToolset[AgentDeps]:
    """运行配置与资源检查相关的 builtin 只读工具。"""
    toolset: FunctionToolset[AgentDeps] = _build_builtin_toolset(
        toolset_id=BUILTIN_RUNTIME_TOOLSET_ID,
        instructions=(
            "Use these builtin tools when the user asks for runtime configuration "
            "or resource availability."
        ),
    )

    # 读取当前 AI runtime 配置摘要，属于“配置查询”类只读工具。
    @toolset.tool(
        metadata=build_tool_metadata(
            category="config",
            readonly=True,
            risk="low",
            approval_required=False,
            tags=["builtin", "system", "readonly", "config", "debug"],
        ),
    )
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

    # 检查当前运行所依赖的核心资源是否可用，属于“运行态健康检查”类只读工具。
    @toolset.tool(
        metadata=build_tool_metadata(
            category="runtime",
            readonly=True,
            risk="low",
            approval_required=False,
            tags=["builtin", "system", "readonly", "runtime", "healthcheck"],
        ),
    )
    async def check_runtime_resources(ctx: RunContext[AgentDeps]) -> dict[str, bool]:
        """Check whether core runtime resources are available for the current run."""
        return {
            "has_request_context": ctx.deps.request is not None,
            "has_db_session_factory": ctx.deps.db_session_factory is not None,
            "has_redis_pool": ctx.deps.redis is not None,
            "has_http_client": ctx.deps.http_client is not None,
        }

    validate_toolset_conventions(toolset)
    return toolset


def get_builtin_toolsets() -> list[FunctionToolset[AgentDeps]]:
    """返回当前 chat-agent 默认挂载的全部 builtin toolsets。

    Agent 侧直接使用这个聚合入口，而不是自己一个个手写拼装，
    这样后面新增或调整 builtin toolset 时，接入点会更稳定。
    """
    return [
        get_builtin_time_toolset(),
        get_builtin_request_toolset(),
        get_builtin_runtime_toolset(),
    ]
