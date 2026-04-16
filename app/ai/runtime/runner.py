from typing import Any

import httpx
import shortuuid
from pydantic_ai import RunContext
from pydantic_ai.usage import RunUsage

from app.ai.config import AISettings
from app.ai.deps import AgentDeps, RequestContext
from app.ai.exceptions import AIDisabledError
from app.ai.runtime.manager import AgentManager
from app.ai.schemas.chat import AgentChatResponse
from app.ai.services.tool_audit import ToolAuditService
from app.db import redis_client
from app.db.session import AsyncSessionLocal


class AgentRunner:
    def __init__(
        self,
        *,
        settings: AISettings,
        agent_manager: AgentManager,
        http_client: httpx.AsyncClient,
        tool_audit: ToolAuditService,
    ) -> None:
        self.settings = settings
        self.agent_manager = agent_manager
        self.http_client = http_client
        self.tool_audit = tool_audit

    async def run_chat(
        self,
        *,
        request_context: RequestContext,
        message: str,
        agent_id: str | None = None,
        session_id: str | None = None,
        model_name: str | None = None,
    ) -> AgentChatResponse:
        """执行一次标准 chat run。

        这里是当前项目里“真正触发 Agent 执行”的核心入口。
        整体职责包括：
        - 检查 AI 开关
        - 解析本次请求要使用的 agent / model
        - 组装 AgentDeps
        - 在执行前记录当前 run 暴露给模型的工具集合
        - 调用 `agent.run(...)`
        - 把结果整理成统一的 API 响应结构
        """
        if not self.settings.enabled:
            raise AIDisabledError("AI 能力已关闭")

        resolved_agent_id = agent_id or self.settings.default_agent
        resolved_model = self.agent_manager.resolve_model(resolved_agent_id, model_name)
        agent = self.agent_manager.get_agent(resolved_agent_id, resolved_model)

        # `AgentDeps` 是本次运行注入给工具层和动态 instructions 的运行时依赖集合。
        # 当前先放 request/settings/db/redis/http_client/tool_audit，
        # 后续继续扩 history / approval / skill / mcp 也会沿着这个入口演进。
        deps = AgentDeps(
            request=request_context,
            settings=self.settings,
            db_session_factory=AsyncSessionLocal,
            redis=redis_client.redis_pool,
            http_client=self.http_client,
            tool_audit=self.tool_audit,
        )

        # 在真正执行前，先把当前 run 对模型可见的工具集合记录下来。
        # 当前记录的是 tool exposure，而不是完整 tool execution telemetry。
        await self._record_tool_exposure(
            agent_id=resolved_agent_id,
            request_id=request_context.request_id,
            message=message,
            agent=agent,
            deps=deps,
        )

        # 真正触发模型调用与工具编排的入口。
        result = await agent.run(message, deps=deps)

        return AgentChatResponse(
            run_id=shortuuid.uuid(),
            agent_id=resolved_agent_id,
            model=resolved_model,
            message=result.output,
            request_id=request_context.request_id,
            session_id=session_id,
            usage=self._serialize_usage(result),
        )

    async def _record_tool_exposure(self, *, agent_id: str, request_id: str, message: str, agent: Any, deps: AgentDeps) -> None:
        """记录当前 run 暴露给模型的工具集合。

        这里的关键点是：不依赖模型是否真的调用了某个工具，
        而是在执行前直接从 Agent 聚合后的 toolset 中读取“本轮可见工具”。

        这样做的价值是：
        - 对真实模型和 TestModel 都一致成立
        - 能稳定回答“这次 run 为什么能看到这些工具”
        - 为后续更细粒度的 wrapper / audit 扩展打基础
        """
        model = agent._get_model(None)
        run_context = RunContext[AgentDeps](
            deps=deps,
            model=model,
            usage=RunUsage(),
            agent=agent,
            prompt=message,
        )

        # 这里拿到的是 Agent 已经聚合完成后的总 toolset，
        # 包括静态挂载的 builtin toolsets，后续也可以扩展到动态 skills / MCP toolsets。
        toolset = agent._get_toolset()
        tools = await toolset.get_tools(run_context)
        self.tool_audit.record_tool_exposure(
            agent_id=agent_id,
            request_id=request_id,
            tool_names=list(tools.keys()),
            tool_metadata={name: dict(tool.tool_def.metadata or {}) for name, tool in tools.items()},
        )

    @staticmethod
    def _serialize_usage(result: Any) -> dict[str, Any] | None:
        """把不同形态的 usage 对象整理成统一字典结构。"""

        usage_value = getattr(result, "usage", None)
        usage = usage_value() if callable(usage_value) else usage_value
        if usage is None:
            return None
        if hasattr(usage, "model_dump"):
            return usage.model_dump(mode="json")
        if hasattr(usage, "__dict__"):
            return dict(vars(usage))
        return {"value": str(usage)}
