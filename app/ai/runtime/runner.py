import json
from typing import Any

import httpx
import shortuuid
from pydantic_ai import ModelMessagesTypeAdapter, RunContext
from pydantic_ai.tools import DeferredToolRequests, DeferredToolResults, ToolApproved, ToolDenied
from pydantic_ai.usage import RunUsage

from app.ai.config import AISettings
from app.ai.deps import AgentDeps, RequestContext
from app.ai.exceptions import AIDisabledError
from app.ai.runtime.manager import AgentManager
from app.ai.schemas.chat import (
    AgentApprovalDecision,
    AgentApprovalRequest,
    AgentChatResponse,
    AgentDeferredToolRequestsPayload,
)
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
        """执行一次标准 chat run。"""

        resolved_agent_id, resolved_model, agent = self._resolve_agent(agent_id=agent_id, model_name=model_name)
        deps = self._build_deps(request_context)

        await self._record_tool_exposure(
            agent_id=resolved_agent_id,
            request_id=request_context.request_id,
            message=message,
            agent=agent,
            deps=deps,
        )

        result = await agent.run(message, deps=deps)
        return self._build_chat_response(
            result=result,
            request_id=request_context.request_id,
            session_id=session_id,
            agent_id=resolved_agent_id,
            model=resolved_model,
        )

    async def resume_chat(
        self,
        *,
        request_context: RequestContext,
        message_history_json: str,
        approvals: list[AgentApprovalDecision],
        agent_id: str | None = None,
        session_id: str | None = None,
        model_name: str | None = None,
    ) -> AgentChatResponse:
        """基于上一轮 deferred tool requests 继续执行一次 run。"""

        resolved_agent_id, resolved_model, agent = self._resolve_agent(agent_id=agent_id, model_name=model_name)
        deps = self._build_deps(request_context)
        message_history = ModelMessagesTypeAdapter.validate_json(message_history_json)
        deferred_tool_results = self._build_deferred_tool_results(approvals)

        result = await agent.run(
            deps=deps,
            message_history=message_history,
            deferred_tool_results=deferred_tool_results,
        )
        return self._build_chat_response(
            result=result,
            request_id=request_context.request_id,
            session_id=session_id,
            agent_id=resolved_agent_id,
            model=resolved_model,
        )

    def _resolve_agent(self, *, agent_id: str | None, model_name: str | None) -> tuple[str, str, Any]:
        if not self.settings.enabled:
            raise AIDisabledError("AI 能力已关闭")

        resolved_agent_id = agent_id or self.settings.default_agent
        resolved_model = self.agent_manager.resolve_model(resolved_agent_id, model_name)
        agent = self.agent_manager.get_agent(resolved_agent_id, resolved_model)
        return resolved_agent_id, resolved_model, agent

    def _build_deps(self, request_context: RequestContext) -> AgentDeps:
        # `AgentDeps` 是本次运行注入给工具层和动态 instructions 的运行时依赖集合。
        # 当前先放 request/settings/db/redis/http_client/tool_audit，
        # 后续继续扩 history / approval / skill / mcp 也会沿着这个入口演进。
        return AgentDeps(
            request=request_context,
            settings=self.settings,
            db_session_factory=AsyncSessionLocal,
            redis=redis_client.redis_pool,
            http_client=self.http_client,
            tool_audit=self.tool_audit,
        )

    def _build_chat_response(
        self,
        *,
        result: Any,
        request_id: str,
        session_id: str | None,
        agent_id: str,
        model: str,
    ) -> AgentChatResponse:
        output = result.output
        response = AgentChatResponse(
            run_id=shortuuid.uuid(),
            agent_id=agent_id,
            model=model,
            status="completed",
            message=None,
            request_id=request_id,
            session_id=session_id,
            usage=self._serialize_usage(result),
        )

        if isinstance(output, DeferredToolRequests):
            response.status = "approval_required"
            response.deferred_tool_requests = self._serialize_deferred_tool_requests(result, output)
            return response

        response.message = output
        return response

    def _serialize_deferred_tool_requests(
        self,
        result: Any,
        output: DeferredToolRequests,
    ) -> AgentDeferredToolRequestsPayload:
        approvals = [
            AgentApprovalRequest(
                tool_call_id=part.tool_call_id or "",
                tool_name=part.tool_name,
                args=self._normalize_tool_args(part.args),
                metadata=dict(output.metadata.get(part.tool_call_id or "", {})),
            )
            for part in output.approvals
        ]
        calls = [
            AgentApprovalRequest(
                tool_call_id=part.tool_call_id or "",
                tool_name=part.tool_name,
                args=self._normalize_tool_args(part.args),
                metadata=dict(output.metadata.get(part.tool_call_id or "", {})),
            )
            for part in output.calls
        ]
        return AgentDeferredToolRequestsPayload(
            approvals=approvals,
            calls=calls,
            message_history_json=result.all_messages_json().decode(),
        )

    @staticmethod
    def _build_deferred_tool_results(approvals: list[AgentApprovalDecision]) -> DeferredToolResults:
        approval_map: dict[str, bool | ToolApproved | ToolDenied] = {}
        for item in approvals:
            if item.approved:
                approval_map[item.tool_call_id] = (
                    ToolApproved(override_args=item.override_args) if item.override_args is not None else True
                )
            else:
                approval_map[item.tool_call_id] = ToolDenied(
                    message=item.denial_message or "The tool call was denied."
                )
        return DeferredToolResults(approvals=approval_map)

    async def _record_tool_exposure(
        self,
        *,
        agent_id: str,
        request_id: str,
        message: str,
        agent: Any,
        deps: AgentDeps,
    ) -> None:
        """记录当前 run 暴露给模型的工具集合。"""

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
    def _normalize_tool_args(args: Any) -> dict[str, Any]:
        if isinstance(args, dict):
            return dict(args)
        if isinstance(args, str):
            try:
                value = json.loads(args)
            except json.JSONDecodeError:
                return {}
            if isinstance(value, dict):
                return value
        return {}

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
