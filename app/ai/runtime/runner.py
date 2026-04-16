import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import shortuuid
from pydantic_ai import ModelMessagesTypeAdapter, RunContext
from pydantic_ai.exceptions import UserError
from pydantic_ai.tools import DeferredToolRequests, DeferredToolResults, ToolApproved, ToolDenied
from pydantic_ai.usage import RunUsage

from app.ai.config import AISettings
from app.ai.deps import AgentDeps, RequestContext
from app.ai.exceptions import AIDisabledError, AIRunExecutionError, AIRuntimeError
from app.ai.runtime.history import SessionHistoryStore
from app.ai.runtime.manager import AgentManager
from app.ai.schemas.chat import (
    AgentApprovalDecision,
    AgentApprovalRequest,
    AgentChatResponse,
    AgentDeferredToolRequestsPayload,
    AgentRunMeta,
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
            history_store: SessionHistoryStore,
    ) -> None:
        self.settings = settings
        self.agent_manager = agent_manager
        self.http_client = http_client
        self.tool_audit = tool_audit
        self.history_store = history_store

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

        如果本次请求带了 `session_id`，会先加载该会话已有的 message history，
        再把本轮运行后的完整消息历史写回存储。
        """

        resolved_agent_id, resolved_model, agent = self._resolve_agent(agent_id=agent_id, model_name=model_name)
        deps = self._build_deps(request_context)
        message_history = await self.history_store.load_messages(session_id)
        history_loaded = bool(message_history)

        await self._record_tool_exposure(
            agent_id=resolved_agent_id,
            request_id=request_context.request_id,
            message=message,
            agent=agent,
            deps=deps,
        )

        try:
            result = await agent.run(message, deps=deps, message_history=message_history or None)
            history_saved = await self._save_history(session_id, result)
        except AIRuntimeError:
            raise
        except Exception as exc:
            raise AIRunExecutionError("chat run 执行失败") from exc
        return self._build_chat_response(
            result=result,
            request_id=request_context.request_id,
            session_id=session_id,
            agent_id=resolved_agent_id,
            model=resolved_model,
            run_kind="chat",
            history_loaded=history_loaded,
            history_saved=history_saved,
        )

    async def run_chat_stream(
            self,
            *,
            request_context: RequestContext,
            message: str,
            agent_id: str | None = None,
            session_id: str | None = None,
            model_name: str | None = None,
    ) -> AsyncIterator[str]:
        """执行一次 SSE 形式的流式 chat run。"""

        resolved_agent_id, resolved_model, agent = self._resolve_agent(agent_id=agent_id, model_name=model_name)
        deps = self._build_deps(request_context)
        message_history = await self.history_store.load_messages(session_id)
        history_loaded = bool(message_history)
        started = False

        await self._record_tool_exposure(
            agent_id=resolved_agent_id,
            request_id=request_context.request_id,
            message=message,
            agent=agent,
            deps=deps,
        )

        try:
            async with agent.run_stream(message, deps=deps, message_history=message_history or None) as stream_result:
                yield self._sse_event(
                    "start",
                    {
                        "run_id": stream_result.run_id,
                        "agent_id": resolved_agent_id,
                        "model": resolved_model,
                        "request_id": request_context.request_id,
                        "session_id": session_id,
                        "meta": self._build_run_meta(
                            run_kind="stream",
                            stream_mode="native",
                            history_loaded=history_loaded,
                            history_saved=False,
                            message_count=len(message_history),
                        ).model_dump(mode="json"),
                    },
                )
                started = True

                try:
                    async for delta in stream_result.stream_text(delta=True):
                        if delta:
                            yield self._sse_event("delta", {"text": delta})
                except UserError:
                    # 当前 `chat-agent` 的输出类型里同时允许 text 和 `DeferredToolRequests`。
                    # 如果这轮 run 走进 approval/deferred 分支，`stream_text()` 不再适用，
                    # 这里转而统一走 `get_output()` 读取最终的 deferred 输出对象。
                    pass

                output = await stream_result.get_output()
                history_saved = await self._save_stream_history(
                    session_id=session_id,
                    messages=stream_result.all_messages(),
                )

                if isinstance(output, DeferredToolRequests):
                    response = self._build_stream_response_from_deferred(
                        stream_result=stream_result,
                        output=output,
                        request_id=request_context.request_id,
                        session_id=session_id,
                        agent_id=resolved_agent_id,
                        model=resolved_model,
                        history_loaded=history_loaded,
                        history_saved=history_saved,
                        stream_mode="native",
                    )
                    yield self._sse_event("approval_required", response.model_dump(mode="json"))
                    return

                response = AgentChatResponse(
                    run_id=stream_result.run_id,
                    agent_id=resolved_agent_id,
                    model=resolved_model,
                    status="completed",
                    message=output,
                    deferred_tool_requests=None,
                    request_id=request_context.request_id,
                    session_id=session_id,
                    usage=self._serialize_usage(stream_result),
                    meta=self._build_run_meta(
                        run_kind="stream",
                        stream_mode="native",
                        history_loaded=history_loaded,
                        history_saved=history_saved,
                        message_count=len(stream_result.all_messages()),
                    ),
                )
                yield self._sse_event("done", response.model_dump(mode="json"))
        except Exception as exc:
            if not started:
                async for event in self._run_chat_stream_fallback(
                        agent=agent,
                        deps=deps,
                        message=message,
                        message_history=message_history,
                        request_id=request_context.request_id,
                        session_id=session_id,
                        agent_id=resolved_agent_id,
                        model=resolved_model,
                        history_loaded=history_loaded,
                        stream_mode="fallback",
                ):
                    yield event
                return

            yield self._sse_event(
                "error",
                self._build_stream_error_payload(
                    error=exc,
                    request_id=request_context.request_id,
                    session_id=session_id,
                    agent_id=resolved_agent_id,
                    model=resolved_model,
                    history_loaded=history_loaded,
                    stream_mode="native",
                ),
            )
            return

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
        """基于上一轮 deferred tool requests 继续执行一次 run。

        当前 `resume` 仍采用无状态协议：
        - 前端回传 `message_history_json`
        - runner 负责把审批结果组装成 `DeferredToolResults`
        - run 完成后，如果带了 `session_id`，也会把新历史写回会话存储
        """

        resolved_agent_id, resolved_model, agent = self._resolve_agent(agent_id=agent_id, model_name=model_name)
        deps = self._build_deps(request_context)
        message_history = ModelMessagesTypeAdapter.validate_json(message_history_json)
        deferred_tool_results = self._build_deferred_tool_results(approvals)

        try:
            result = await agent.run(
                deps=deps,
                message_history=message_history,
                deferred_tool_results=deferred_tool_results,
            )
            history_saved = await self._save_history(session_id, result)
        except AIRuntimeError:
            raise
        except Exception as exc:
            raise AIRunExecutionError("resume run 执行失败") from exc
        return self._build_chat_response(
            result=result,
            request_id=request_context.request_id,
            session_id=session_id,
            agent_id=resolved_agent_id,
            model=resolved_model,
            run_kind="resume",
            history_loaded=True,
            history_saved=history_saved,
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
            run_kind: str,
            history_loaded: bool,
            history_saved: bool,
    ) -> AgentChatResponse:
        output = result.output
        response = AgentChatResponse(
            run_id=self._extract_run_id(result),
            agent_id=agent_id,
            model=model,
            status="completed",
            message=None,
            request_id=request_id,
            session_id=session_id,
            usage=self._serialize_usage(result),
            meta=self._build_run_meta(
                run_kind=run_kind,
                stream_mode=None,
                history_loaded=history_loaded,
                history_saved=history_saved,
                message_count=len(result.all_messages()),
            ),
        )

        if isinstance(output, DeferredToolRequests):
            response.status = "approval_required"
            response.deferred_tool_requests = self._serialize_deferred_tool_requests(result, output)
            return response

        response.message = output
        return response

    async def _run_chat_stream_fallback(
            self,
            *,
            agent: Any,
            deps: AgentDeps,
            message: str,
            message_history: list[Any],
            request_id: str,
            session_id: str | None,
            agent_id: str,
            model: str,
            history_loaded: bool,
            stream_mode: str,
    ) -> AsyncIterator[str]:
        """当模型不支持真正的 streamed request 时，退化成单次 run 再包装成 SSE。"""

        try:
            result = await agent.run(message, deps=deps, message_history=message_history or None)
            history_saved = await self._save_history(session_id, result)
        except Exception as exc:
            yield self._sse_event(
                "error",
                self._build_stream_error_payload(
                    error=exc,
                    request_id=request_id,
                    session_id=session_id,
                    agent_id=agent_id,
                    model=model,
                    history_loaded=history_loaded,
                    stream_mode="fallback",
                ),
            )
            return
        response = self._build_chat_response(
            result=result,
            request_id=request_id,
            session_id=session_id,
            agent_id=agent_id,
            model=model,
            run_kind="stream",
            history_loaded=history_loaded,
            history_saved=history_saved,
        )
        response.meta.stream_mode = stream_mode
        yield self._sse_event(
            "start",
            {
                "run_id": response.run_id,
                "agent_id": agent_id,
                "model": model,
                "request_id": request_id,
                "session_id": session_id,
                "meta": self._build_run_meta(
                    run_kind="stream",
                    stream_mode="fallback",
                    history_loaded=history_loaded,
                    history_saved=False,
                    message_count=len(message_history),
                ).model_dump(mode="json"),
            },
        )
        event_name = "approval_required" if response.status == "approval_required" else "done"
        yield self._sse_event(event_name, response.model_dump(mode="json"))

    def _build_stream_response_from_deferred(
            self,
            *,
            stream_result: Any,
            output: DeferredToolRequests,
            request_id: str,
            session_id: str | None,
            agent_id: str,
            model: str,
            history_loaded: bool,
            history_saved: bool,
            stream_mode: str,
    ) -> AgentChatResponse:
        return AgentChatResponse(
            run_id=stream_result.run_id,
            agent_id=agent_id,
            model=model,
            status="approval_required",
            message=None,
            deferred_tool_requests=self._serialize_deferred_tool_requests(stream_result, output),
            request_id=request_id,
            session_id=session_id,
            usage=self._serialize_usage(stream_result),
            meta=self._build_run_meta(
                run_kind="stream",
                stream_mode=stream_mode,
                history_loaded=history_loaded,
                history_saved=history_saved,
                message_count=len(stream_result.all_messages()),
            ),
        )

    async def _save_history(self, session_id: str | None, result: Any) -> bool:
        """把本轮 run 结束后的完整消息历史写回会话存储。"""

        if not session_id:
            return False
        await self.history_store.save_messages(session_id, result.all_messages())
        return True

    async def _save_stream_history(self, *, session_id: str | None, messages: list[Any]) -> bool:
        """流式 run 完成后写回完整消息历史。"""

        if not session_id:
            return False
        await self.history_store.save_messages(session_id, messages)
        return True

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

    @staticmethod
    def _build_run_meta(
            *,
            run_kind: str,
            stream_mode: str | None,
            history_loaded: bool,
            history_saved: bool,
            message_count: int,
    ) -> AgentRunMeta:
        return AgentRunMeta(
            run_kind=run_kind,
            stream_mode=stream_mode,
            history_loaded=history_loaded,
            history_saved=history_saved,
            message_count=message_count,
        )

    @staticmethod
    def _sse_event(event: str, data: dict[str, Any]) -> str:
        payload = json.dumps(data, ensure_ascii=False)
        return f"event: {event}\ndata: {payload}\n\n"

    @staticmethod
    def _extract_run_id(result: Any) -> str:
        run_id = getattr(result, "run_id", None)
        if isinstance(run_id, str) and run_id:
            return run_id
        return shortuuid.uuid()

    def _build_stream_error_payload(
            self,
            *,
            error: Exception,
            request_id: str,
            session_id: str | None,
            agent_id: str,
            model: str,
            history_loaded: bool,
            stream_mode: str,
    ) -> dict[str, Any]:
        return {
            "error": str(error),
            "error_type": type(error).__name__,
            "request_id": request_id,
            "session_id": session_id,
            "agent_id": agent_id,
            "model": model,
            "meta": self._build_run_meta(
                run_kind="stream",
                stream_mode=stream_mode,
                history_loaded=history_loaded,
                history_saved=False,
                message_count=0,
            ).model_dump(mode="json"),
        }
