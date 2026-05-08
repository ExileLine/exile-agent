import json
import asyncio
import time
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any

import httpx
import shortuuid
from pydantic_ai import ModelMessagesTypeAdapter, RunContext
from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    PartDeltaEvent,
    PartStartEvent,
    RetryPromptPart,
    ModelRequest,
    ModelResponse,
    TextPart,
    TextPartDelta,
    UserPromptPart,
)
from pydantic_ai.run import AgentRunResultEvent
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.toolsets.abstract import AbstractToolset
from pydantic_ai.tools import DeferredToolRequests, DeferredToolResults, ToolApproved, ToolDenied
from pydantic_ai.usage import RunUsage

from app.ai.config import AISettings
from app.ai.config_store import AICapabilityResolver, AIConfigRepository
from app.ai.config_store.encryption import decrypt_secret
from app.ai.deps import AgentDeps, RequestContext
from app.ai.exceptions import (
    AIDisabledError,
    AgentNotFoundError,
    AIConfigValidationError,
    AIRunExecutionError,
    AIRuntimeError,
    MCPConfigurationError,
    MCPRuntimeError,
)
from app.ai.mcp import (
    MCPManager,
    ManagedMCPServerConfig,
    ManagedMCPServerSSEConfig,
    ManagedMCPServerStdioConfig,
    ManagedMCPServerStreamableHTTPConfig,
)
from app.ai.runtime.agent_router import AgentRouter
from app.ai.runtime.approvals import ApprovalRecord, ApprovalStore
from app.ai.runtime.history import SessionHistoryStore
from app.ai.runtime.manager import AgentManager
from app.ai.runtime.resolved_config import ResolvedAgentRoute, ResolvedMCPServerConfig, ResolvedModelConfig, ResolvedRunConfig
from app.ai.runtime.team_trace import TeamRunTracePayload, TeamRunTraceStore
from app.ai.schemas.chat import (
    AgentApprovalDecision,
    AgentApprovalRequest,
    AgentChatResponse,
    AgentDeferredToolRequestsPayload,
    AgentRunMeta,
)
from app.ai.skills import SkillRegistry, SkillResolution, SkillResolver
from app.ai.services.tool_audit import ToolAuditService
from app.ai.toolsets import (
    build_registered_toolsets,
    wrap_toolsets_with_audit,
    wrap_toolsets_with_metadata_approval,
)
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
            approval_store: ApprovalStore,
            mcp_manager: MCPManager | None,
            skill_registry: SkillRegistry | None,
            skill_resolver: SkillResolver | None,
            team_trace_store: TeamRunTraceStore | None = None,
            enable_config_resolver: bool = False,
    ) -> None:
        self.settings = settings
        self.agent_manager = agent_manager
        self.http_client = http_client
        self.tool_audit = tool_audit
        self.history_store = history_store
        self.approval_store = approval_store
        self.team_trace_store = team_trace_store
        self.agent_router = AgentRouter()
        self.mcp_manager = mcp_manager
        self.skill_registry = skill_registry
        self.skill_resolver = skill_resolver
        self.enable_config_resolver = enable_config_resolver

    async def run_chat(
            self,
            *,
            request_context: RequestContext,
            message: str,
            agent_id: str | None = None,
            session_id: str | None = None,
            model_name: str | None = None,
            mcp_server_ids: list[str] | None = None,
            skill_ids: list[str] | None = None,
            skill_tags: list[str] | None = None,
    ) -> AgentChatResponse:
        """执行一次标准 chat run。

        如果本次请求带了 `session_id`，会先加载该会话已有的 message history，
        再把本轮运行后的完整消息历史写回存储。
        """

        run_config = await self._resolve_run_config(
            agent_id=agent_id,
            model_name=model_name,
            mcp_server_ids=mcp_server_ids,
            skill_ids=skill_ids,
            route_message=message,
            allow_agent_routing=True,
            allow_parallel_routing=True,
        )
        if self._should_run_parallel_team(run_config):
            return await self._run_parallel_agent_team(
                request_context=request_context,
                message=message,
                session_id=session_id,
                model_name=model_name,
                mcp_server_ids=mcp_server_ids,
                skill_ids=skill_ids,
                skill_tags=skill_tags,
                route_run_config=run_config,
            )

        resolved_agent_id, resolved_model, agent = self._resolve_agent(run_config)
        skill_resolution = self._resolve_skills(
            agent_id=resolved_agent_id,
            message=message,
            skill_ids=list(run_config.skill_ids),
            skill_tags=skill_tags,
        )
        deps = self._build_deps(request_context, resolved_skill_names=tuple(skill_resolution.skill_names))
        resolved_mcp_server_ids, run_toolsets = self._resolve_request_toolsets(
            mcp_server_ids=run_config.mcp_server_keys,
            mcp_server_configs=run_config.mcp_servers if run_config.source == "database" else (),
            route_message=message,
            skill_resolution=skill_resolution,
            allow_auto_route=run_config.source != "database",
        )
        message_history = await self.history_store.load_messages(
            session_id,
            request_context=request_context,
            agent_id=resolved_agent_id,
        )
        history_loaded = bool(message_history)

        await self._record_tool_exposure(
            agent_id=resolved_agent_id,
            request_id=request_context.request_id,
            message=message,
            agent=agent,
            deps=deps,
            additional_toolsets=run_toolsets,
            resolved_mcp_server_ids=resolved_mcp_server_ids,
        )

        try:
            result = await agent.run(
                message,
                deps=deps,
                message_history=message_history or None,
                instructions=skill_resolution.instructions or None,
                toolsets=run_toolsets or None,
            )
            history_saved = await self._save_history(
                session_id=session_id,
                result=result,
                request_context=request_context,
                agent_id=resolved_agent_id,
                model=resolved_model,
                mcp_servers=resolved_mcp_server_ids,
                skills=skill_resolution.skill_names,
            )
        except AIRuntimeError:
            raise
        except Exception as exc:
            raise AIRunExecutionError("chat run 执行失败") from exc
        response = self._build_chat_response(
            result=result,
            request_id=request_context.request_id,
            session_id=session_id,
            agent_id=resolved_agent_id,
            model=resolved_model,
            run_kind="chat",
            history_loaded=history_loaded,
            history_saved=history_saved,
            mcp_servers=resolved_mcp_server_ids,
            skills=skill_resolution.skill_names,
            run_config=run_config,
        )
        await self._attach_approval_record(response, request_context)
        return response

    async def run_chat_stream(
            self,
            *,
            request_context: RequestContext,
            message: str,
            agent_id: str | None = None,
            session_id: str | None = None,
            model_name: str | None = None,
            mcp_server_ids: list[str] | None = None,
            skill_ids: list[str] | None = None,
            skill_tags: list[str] | None = None,
    ) -> AsyncIterator[str]:
        """执行一次 SSE 形式的流式 chat run。

        当前实现优先走 `agent.run_stream_events(...)`：
        - 可以同时拿到文本增量、工具调用、工具结果、最终 run result
        - 再由 runner 统一翻译成前端可消费的 SSE 事件

        如果底层模型不支持真正的 streamed request，则退化到 fallback：
        - 内部改走一次普通 `agent.run(...)`
        - 再把最终结果包装成 `start -> done` 或审批事件
        """

        run_config = await self._resolve_run_config(
            agent_id=agent_id,
            model_name=model_name,
            mcp_server_ids=mcp_server_ids,
            skill_ids=skill_ids,
            route_message=message,
            allow_agent_routing=True,
            allow_parallel_routing=True,
        )
        if self._should_run_parallel_team(run_config):
            async for event in self._run_parallel_agent_team_stream(
                    request_context=request_context,
                    message=message,
                    session_id=session_id,
                    model_name=model_name,
                    mcp_server_ids=mcp_server_ids,
                    skill_ids=skill_ids,
                    skill_tags=skill_tags,
                    route_run_config=run_config,
            ):
                yield event
            return

        resolved_agent_id, resolved_model, agent = self._resolve_agent(run_config)
        skill_resolution = self._resolve_skills(
            agent_id=resolved_agent_id,
            message=message,
            skill_ids=list(run_config.skill_ids),
            skill_tags=skill_tags,
        )
        deps = self._build_deps(request_context, resolved_skill_names=tuple(skill_resolution.skill_names))
        resolved_mcp_server_ids, run_toolsets = self._resolve_request_toolsets(
            mcp_server_ids=run_config.mcp_server_keys,
            mcp_server_configs=run_config.mcp_servers if run_config.source == "database" else (),
            route_message=message,
            skill_resolution=skill_resolution,
            allow_auto_route=run_config.source != "database",
        )
        message_history = await self.history_store.load_messages(
            session_id,
            request_context=request_context,
            agent_id=resolved_agent_id,
        )
        history_loaded = bool(message_history)

        tool_metadata_by_name = await self._record_tool_exposure(
            agent_id=resolved_agent_id,
            request_id=request_context.request_id,
            message=message,
            agent=agent,
            deps=deps,
            additional_toolsets=run_toolsets,
            resolved_mcp_server_ids=resolved_mcp_server_ids,
        )

        try:
            if not run_config.runtime_flags.get("supports_stream", True):
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
                        mcp_servers=resolved_mcp_server_ids,
                        run_toolsets=run_toolsets,
                        instructions=skill_resolution.instructions,
                        skills=skill_resolution.skill_names,
                        run_config=run_config,
                        user_id=request_context.user_id,
                        tenant_id=request_context.tenant_id,
                ):
                    yield event
                return

            # `run_stream_events()` 是这次改造的核心：
            # 它不是只吐文本，而是会产出 PydanticAI 的统一运行事件流，
            # 包括文本 part、FunctionToolCallEvent、FunctionToolResultEvent、
            # 以及最后的 AgentRunResultEvent。
            stream = agent.run_stream_events(
                message,
                deps=deps,
                message_history=message_history or None,
                instructions=skill_resolution.instructions or None,
                toolsets=run_toolsets or None,
            )
            try:
                # 先探测首个事件有两个目的：
                # 1. 尽早确认 native streaming 能否真正启动
                # 2. 拿到可能已经带有 run_id 的最终事件，避免 start 事件没有稳定 run_id
                first_event = await anext(stream)
            except StopAsyncIteration:
                return
            except Exception:
                # 这里的异常通常意味着当前模型/测试模型并没有真正支持 streamed request，
                # 所以直接切到 fallback，而不是把整个接口报错给前端。
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
                        mcp_servers=resolved_mcp_server_ids,
                        run_toolsets=run_toolsets,
                        instructions=skill_resolution.instructions,
                        skills=skill_resolution.skill_names,
                        run_config=run_config,
                        user_id=request_context.user_id,
                        tenant_id=request_context.tenant_id,
                ):
                    yield event
                return

            # `run_stream_events()` 的首个事件不一定带 run_id。
            # 如果拿不到，就先生成一个本地 run_id，保证整条 SSE 会话有稳定标识。
            stream_run_id = self._extract_stream_run_id_from_event(first_event) or shortuuid.uuid()
            yield self._sse_event(
                "start",
                {
                    "run_id": stream_run_id,
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
                        mcp_servers=resolved_mcp_server_ids,
                        skills=skill_resolution.skill_names,
                        run_config=run_config,
                    ).model_dump(mode="json"),
                },
            )

            # 把 PydanticAI 的底层事件翻译成我们自己的稳定 SSE 协议。
            # 前端只需要理解这些事件名，不需要直接感知 PydanticAI 的内部事件结构。
            async for event in self._iterate_stream_events(first_event, stream):
                if isinstance(event, PartStartEvent) and isinstance(event.part, TextPart) and event.part.content:
                    yield self._sse_event("delta", {"run_id": stream_run_id, "text": event.part.content})
                    continue

                if (
                        isinstance(event, PartDeltaEvent)
                        and isinstance(event.delta, TextPartDelta)
                        and event.delta.content_delta
                ):
                    yield self._sse_event("delta", {"run_id": stream_run_id, "text": event.delta.content_delta})
                    continue

                if isinstance(event, FunctionToolCallEvent):
                    yield self._sse_event(
                        "tool_call",
                        self._build_stream_tool_call_payload(
                            run_id=stream_run_id,
                            event=event,
                            tool_metadata_by_name=tool_metadata_by_name,
                        ),
                    )
                    continue

                if isinstance(event, FunctionToolResultEvent):
                    yield self._sse_event(
                        "tool_result",
                        self._build_stream_tool_result_payload(
                            run_id=stream_run_id,
                            event=event,
                            tool_metadata_by_name=tool_metadata_by_name,
                        ),
                    )
                    continue

                if isinstance(event, AgentRunResultEvent):
                    # 真正的“本轮运行结束”信号在这里。
                    # 在此之前，前面的 delta/tool_call/tool_result 都只是过程事件。
                    result = event.result
                    history_saved = await self._save_history(
                        session_id=session_id,
                        result=result,
                        request_context=request_context,
                        agent_id=resolved_agent_id,
                        model=resolved_model,
                        mcp_servers=resolved_mcp_server_ids,
                        skills=skill_resolution.skill_names,
                    )
                    response = self._build_chat_response(
                        result=result,
                        request_id=request_context.request_id,
                        session_id=session_id,
                        agent_id=resolved_agent_id,
                        model=resolved_model,
                        run_kind="stream",
                        history_loaded=history_loaded,
                        history_saved=history_saved,
                        mcp_servers=resolved_mcp_server_ids,
                        skills=skill_resolution.skill_names,
                        run_config=run_config,
                    )
                    response.meta.stream_mode = "native"
                    await self._attach_approval_record(response, request_context)

                    if response.status == "approval_required":
                        payload = response.model_dump(mode="json")
                        # `approval_pending` 是新增的更语义化事件，
                        # 但为了兼容已有前端协议，仍然继续发一份 `approval_required`。
                        yield self._sse_event("approval_pending", payload)
                        yield self._sse_event("approval_required", payload)
                        return

                    yield self._sse_event("done", response.model_dump(mode="json"))
                    return
        except Exception as exc:
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
                    mcp_servers=resolved_mcp_server_ids,
                    skills=skill_resolution.skill_names,
                    run_config=run_config,
                ),
            )
            return

    async def resume_chat(
            self,
            *,
            request_context: RequestContext,
            message_history_json: str | None,
            approvals: list[AgentApprovalDecision],
            approval_id: str | None = None,
            agent_id: str | None = None,
            session_id: str | None = None,
            model_name: str | None = None,
            mcp_server_ids: list[str] | None = None,
            skill_ids: list[str] | None = None,
            skill_tags: list[str] | None = None,
    ) -> AgentChatResponse:
        """基于上一轮 deferred tool requests 继续执行一次 run。

        当前 `resume` 支持两种协议：
        - 新协议：前端回传 `approval_id`
        - 旧协议：前端回传 `message_history_json`
        - runner 负责把审批结果组装成 `DeferredToolResults`
        - run 完成后，如果带了 `session_id`，也会把新历史写回会话存储
        """

        approval_record = await self._load_approval_record_for_resume(
            approval_id=approval_id,
            agent_id=agent_id,
            session_id=session_id,
            user_id=request_context.user_id,
        )
        resolved_message_history_json = (
            approval_record.message_history_json if approval_record is not None else message_history_json
        )
        if not resolved_message_history_json:
            raise AIConfigValidationError("resume 需要 approval_id 或 message_history_json")
        self._validate_approval_decisions(approvals=approvals, approval_record=approval_record)
        if approval_record is not None and approval_record.metadata.get("kind") == "team_worker_approval":
            return await self._resume_parallel_team_worker(
                request_context=request_context,
                approval_record=approval_record,
                approvals=approvals,
                session_id=session_id,
                model_name=model_name,
            )

        message_history = ModelMessagesTypeAdapter.validate_json(resolved_message_history_json)
        latest_user_message = self._extract_latest_user_message(message_history)
        resume_agent_id = agent_id or (approval_record.agent_id if approval_record is not None else None)
        run_config = await self._resolve_run_config(
            agent_id=resume_agent_id,
            model_name=model_name,
            mcp_server_ids=mcp_server_ids,
            skill_ids=skill_ids,
            route_message=latest_user_message,
            allow_agent_routing=False,
            allow_parallel_routing=False,
        )
        resolved_agent_id, resolved_model, agent = self._resolve_agent(run_config)
        skill_resolution = self._resolve_skills(
            agent_id=resolved_agent_id,
            message=latest_user_message,
            skill_ids=list(run_config.skill_ids),
            skill_tags=skill_tags,
        )
        deps = self._build_deps(request_context, resolved_skill_names=tuple(skill_resolution.skill_names))
        resolved_mcp_server_ids, run_toolsets = self._resolve_request_toolsets(
            mcp_server_ids=run_config.mcp_server_keys,
            mcp_server_configs=run_config.mcp_servers if run_config.source == "database" else (),
            route_message=latest_user_message,
            skill_resolution=skill_resolution,
            allow_auto_route=run_config.source != "database",
        )
        deferred_tool_results = self._build_deferred_tool_results(approvals)

        try:
            result = await agent.run(
                deps=deps,
                message_history=message_history,
                deferred_tool_results=deferred_tool_results,
                instructions=skill_resolution.instructions or None,
                toolsets=run_toolsets or None,
            )
            history_saved = await self._save_history(
                session_id=session_id,
                result=result,
                request_context=request_context,
                agent_id=resolved_agent_id,
                model=resolved_model,
                mcp_servers=resolved_mcp_server_ids,
                skills=skill_resolution.skill_names,
            )
        except AIRuntimeError:
            raise
        except Exception as exc:
            raise AIRunExecutionError("resume run 执行失败") from exc
        response = self._build_chat_response(
            result=result,
            request_id=request_context.request_id,
            session_id=session_id,
            agent_id=resolved_agent_id,
            model=resolved_model,
            run_kind="resume",
            history_loaded=True,
            history_saved=history_saved,
            mcp_servers=resolved_mcp_server_ids,
            skills=skill_resolution.skill_names,
            run_config=run_config,
        )
        if approval_record is not None:
            await self.approval_store.mark_completed(approval_record.approval_id)
        await self._attach_approval_record(response, request_context)
        return response

    async def _resolve_run_config(
            self,
            *,
            agent_id: str | None,
            model_name: str | None,
            mcp_server_ids: list[str] | None,
            skill_ids: list[str] | None,
            route_message: str | None = None,
            allow_agent_routing: bool = True,
            allow_parallel_routing: bool = False,
    ) -> ResolvedRunConfig:
        """解析本轮 run 的控制面配置。

        未启用数据库控制面时，返回与旧逻辑一致的 settings fallback；
        启用后通过 AICapabilityResolver 校验模型与 MCP allowlist。
        """

        if not self.settings.enabled:
            raise AIDisabledError("AI 能力已关闭")

        agent_route = self._resolve_agent_route(
            agent_id=agent_id,
            route_message=route_message,
            allow_agent_routing=allow_agent_routing,
            allow_parallel_routing=allow_parallel_routing,
        )
        selected_agent_id = (
            agent_route.worker_agent_ids[0]
            if agent_route.mode == "parallel" and agent_route.worker_agent_ids
            else agent_route.selected_agent_id
        )

        if not self.enable_config_resolver:
            run_config = self._build_settings_fallback_run_config(
                agent_id=selected_agent_id,
                model_name=model_name,
                mcp_server_ids=mcp_server_ids,
                skill_ids=skill_ids,
            )
            return replace(run_config, agent_route=agent_route)

        async with AsyncSessionLocal() as session:
            resolver = AICapabilityResolver(
                settings=self.settings,
                repository=AIConfigRepository(session),
            )
            run_config = await resolver.resolve(
                agent_id=selected_agent_id,
                requested_model=model_name,
                requested_mcp_servers=mcp_server_ids,
                requested_skill_ids=skill_ids,
                route_message=route_message,
            )
            return replace(run_config, agent_route=agent_route)

    def _resolve_agent_route(
            self,
            *,
            agent_id: str | None,
            route_message: str | None,
            allow_agent_routing: bool,
            allow_parallel_routing: bool,
    ) -> ResolvedAgentRoute:
        if allow_agent_routing:
            route = self.agent_router.resolve(
                requested_agent_id=agent_id,
                message=route_message,
                default_agent_id=self.settings.default_agent,
            )
            if allow_parallel_routing or route.mode != "parallel":
                return route
            first_worker_agent_id = route.worker_agent_ids[0] if route.worker_agent_ids else route.selected_agent_id
            return ResolvedAgentRoute(
                requested_agent_id=route.requested_agent_id,
                selected_agent_id=first_worker_agent_id,
                source=route.source,
                reason=f"{route.reason}；当前接口使用单 Agent 路由",
                matched_keywords=route.matched_keywords,
                candidate_agent_ids=route.candidate_agent_ids,
            )
        selected_agent_id = agent_id or self.settings.default_agent
        return ResolvedAgentRoute(
            requested_agent_id=agent_id,
            selected_agent_id=selected_agent_id,
            source="explicit" if agent_id else "default",
            reason="续跑阶段不重新执行 AgentRouter",
        )

    def _build_settings_fallback_run_config(
            self,
            *,
            agent_id: str | None,
            model_name: str | None,
            mcp_server_ids: list[str] | None,
            skill_ids: list[str] | None,
    ) -> ResolvedRunConfig:
        resolved_agent_id = agent_id or self.settings.default_agent
        resolved_model = self.agent_manager.resolve_model(resolved_agent_id, model_name)
        return ResolvedRunConfig(
            agent_id=resolved_agent_id,
            model=ResolvedModelConfig(
                model_key=resolved_model,
                provider_key=None,
                model_name=resolved_model,
            ),
            mcp_servers=tuple(
                ResolvedMCPServerConfig(
                    server_key=server_id,
                    transport="settings_fallback",
                    tool_prefix=None,
                )
                for server_id in _dedupe_server_ids(mcp_server_ids or [])
            ),
            skill_ids=tuple(_dedupe_server_ids(skill_ids or [])),
            source="settings_fallback",
            runtime_flags={
                "allow_request_model_override": True,
                "allow_request_mcp_override": True,
                "supports_stream": True,
            },
        )

    def _resolve_agent(self, run_config: ResolvedRunConfig) -> tuple[str, str, Any]:
        if not self.settings.enabled:
            raise AIDisabledError("AI 能力已关闭")

        resolved_agent_id = run_config.agent_id
        resolved_model = run_config.model_name
        runtime_model = self._build_runtime_model(run_config)
        model_cache_key = self._build_model_cache_key(run_config)
        try:
            agent = self.agent_manager.get_agent(
                resolved_agent_id,
                resolved_model,
                model=runtime_model,
                model_cache_key=model_cache_key,
            )
        except AgentNotFoundError:
            if run_config.source != "database":
                raise
            # 数据库控制面允许创建多个业务 Agent 配置；当前代码层只有默认 builder。
            # 因此 DB Agent 缺少同名静态注册时，复用默认 Agent builder，但保留原 agent_id 做治理维度。
            agent = self.agent_manager.get_agent(
                resolved_agent_id,
                resolved_model,
                runtime_agent_id=self.settings.default_agent,
                model=runtime_model,
                model_cache_key=model_cache_key,
            )
        return resolved_agent_id, resolved_model, agent

    @staticmethod
    def _should_run_parallel_team(run_config: ResolvedRunConfig) -> bool:
        route = run_config.agent_route
        return bool(route and route.mode == "parallel" and len(route.worker_agent_ids) >= 2)

    async def _run_parallel_agent_team(
            self,
            *,
            request_context: RequestContext,
            message: str,
            session_id: str | None,
            model_name: str | None,
            mcp_server_ids: list[str] | None,
            skill_ids: list[str] | None,
            skill_tags: list[str] | None,
            route_run_config: ResolvedRunConfig,
    ) -> AgentChatResponse:
        started_at = time.perf_counter()
        team_run_id = shortuuid.uuid()
        route = route_run_config.agent_route
        if route is None or not route.worker_agent_ids or route.aggregator_agent_id is None:
            raise AIConfigValidationError("Agent 并行协同路由配置不完整")

        previous_history_messages = await self.history_store.load_messages(
            session_id,
            request_context=request_context,
            agent_id="team:auto-parallel",
        )
        worker_context = self._extract_latest_parallel_team_summary(previous_history_messages)
        worker_outputs = await asyncio.gather(
            *[
                self._run_parallel_worker(
                    agent_id=worker_agent_id,
                    request_context=request_context,
                    message=message,
                    session_id=session_id,
                    model_name=model_name,
                    mcp_server_ids=mcp_server_ids,
                    skill_ids=skill_ids,
                    skill_tags=skill_tags,
                    team_context=worker_context,
                )
                for worker_agent_id in route.worker_agent_ids
            ]
        )
        completed_worker_outputs = [item for item in worker_outputs if item.get("status") == "completed"]
        approval_worker_outputs = [item for item in worker_outputs if item.get("status") == "approval_required"]
        failed_worker_outputs = [item for item in worker_outputs if item.get("status") == "failed"]
        if approval_worker_outputs:
            approval_response = await self._build_parallel_team_approval_response(
                team_run_id=team_run_id,
                request_context=request_context,
                session_id=session_id,
                route_run_config=route_run_config,
                route=route,
                message=message,
                worker_outputs=worker_outputs,
                approval_worker_output=approval_worker_outputs[0],
                history_loaded=bool(previous_history_messages),
                started_at=started_at,
                model=route_run_config.model_name,
                run_kind="chat",
                mcp_server_ids=mcp_server_ids,
                skill_ids=skill_ids,
                skill_tags=skill_tags,
                model_name=model_name,
            )
            return approval_response

        if not completed_worker_outputs:
            failed_agents = ", ".join(str(item.get("agent_id")) for item in failed_worker_outputs)
            await self._save_team_run_trace(
                team_run_id=team_run_id,
                run_kind="chat",
                request_context=request_context,
                session_id=session_id,
                route_run_config=route_run_config,
                aggregator_agent_id=route.aggregator_agent_id,
                model=route_run_config.model_name,
                status="failed",
                final_message=None,
                usage=None,
                worker_outputs=worker_outputs,
                started_at=started_at,
                error=f"并行协同所有 worker 均失败: {failed_agents}",
            )
            raise AIConfigValidationError(f"并行协同所有 worker 均失败: {failed_agents}")
        aggregator_output = await self._run_parallel_aggregator(
            aggregator_agent_id=route.aggregator_agent_id,
            request_context=request_context,
            message=message,
            worker_outputs=worker_outputs,
            model_name=model_name,
            message_history=previous_history_messages,
        )
        history_messages = await self._build_parallel_team_history_messages(
            previous_messages=previous_history_messages,
            message=message,
            final_message=aggregator_output["message"],
        )
        history_saved = await self._save_parallel_team_history(
            session_id=session_id,
            request_context=request_context,
            messages=history_messages,
            model=aggregator_output["model"],
            team_results=worker_outputs,
            aggregator_agent_id=route.aggregator_agent_id,
            route_reason=route.reason,
        )
        response = AgentChatResponse(
            run_id=team_run_id,
            agent_id=route.selected_agent_id,
            model=aggregator_output["model"],
            status="completed",
            message=aggregator_output["message"],
            request_id=request_context.request_id,
            session_id=session_id,
            usage=aggregator_output["usage"],
            meta=self._build_run_meta(
                run_kind="chat",
                stream_mode=None,
                history_loaded=bool(previous_history_messages),
                history_saved=history_saved,
                message_count=len(history_messages),
                mcp_servers=[],
                skills=[],
                run_config=route_run_config,
                team_results=worker_outputs,
            ),
        )
        await self._save_team_run_trace(
            team_run_id=team_run_id,
            run_kind="chat",
            request_context=request_context,
            session_id=session_id,
            route_run_config=route_run_config,
            aggregator_agent_id=route.aggregator_agent_id,
            model=aggregator_output["model"],
            status=response.status,
            final_message=response.message,
            usage=response.usage,
            worker_outputs=worker_outputs,
            started_at=started_at,
        )
        return response

    async def _run_parallel_agent_team_stream(
            self,
            *,
            request_context: RequestContext,
            message: str,
            session_id: str | None,
            model_name: str | None,
            mcp_server_ids: list[str] | None,
            skill_ids: list[str] | None,
            skill_tags: list[str] | None,
            route_run_config: ResolvedRunConfig,
    ) -> AsyncIterator[str]:
        started_at = time.perf_counter()
        route = route_run_config.agent_route
        stream_run_id = shortuuid.uuid()
        if route is None or not route.worker_agent_ids or route.aggregator_agent_id is None:
            yield self._sse_event(
                "error",
                self._build_stream_error_payload(
                    error=AIConfigValidationError("Agent 并行协同路由配置不完整"),
                    request_id=request_context.request_id,
                    session_id=session_id,
                    agent_id="team:auto-parallel",
                    model=route_run_config.model_name,
                    history_loaded=False,
                    stream_mode="fallback",
                    mcp_servers=[],
                    skills=[],
                    run_config=route_run_config,
                ),
            )
            return

        previous_history_messages = await self.history_store.load_messages(
            session_id,
            request_context=request_context,
            agent_id="team:auto-parallel",
        )
        history_loaded = bool(previous_history_messages)
        worker_context = self._extract_latest_parallel_team_summary(previous_history_messages)

        yield self._sse_event(
            "start",
            {
                "run_id": stream_run_id,
                "agent_id": route.selected_agent_id,
                "model": route_run_config.model_name,
                "request_id": request_context.request_id,
                "session_id": session_id,
                "meta": self._build_run_meta(
                    run_kind="stream",
                    stream_mode="fallback",
                    history_loaded=history_loaded,
                    history_saved=False,
                    message_count=len(previous_history_messages),
                    mcp_servers=[],
                    skills=[],
                    run_config=route_run_config,
                ).model_dump(mode="json"),
            },
        )

        worker_tasks: dict[asyncio.Task[dict[str, Any]], str] = {}
        for worker_agent_id in route.worker_agent_ids:
            yield self._sse_event(
                "team_worker_start",
                {
                    "run_id": stream_run_id,
                    "agent_id": worker_agent_id,
                    "role": self._parallel_agent_role(worker_agent_id),
                },
            )
            task = asyncio.create_task(
                self._run_parallel_worker(
                    agent_id=worker_agent_id,
                    request_context=request_context,
                    message=message,
                    session_id=session_id,
                    model_name=model_name,
                    mcp_server_ids=mcp_server_ids,
                    skill_ids=skill_ids,
                    skill_tags=skill_tags,
                    team_context=worker_context,
                )
            )
            worker_tasks[task] = worker_agent_id

        worker_outputs: list[dict[str, Any]] = []
        pending = set(worker_tasks)
        while pending:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                worker_agent_id = worker_tasks[task]
                try:
                    worker_output = task.result()
                except Exception as exc:
                    worker_output = self._build_parallel_worker_result(
                        agent_id=worker_agent_id,
                        model=model_name or "",
                        status="failed",
                        message=None,
                        usage=None,
                        mcp_servers=[],
                        skills=[],
                        started_at=time.perf_counter(),
                        error=str(exc),
                        error_type=type(exc).__name__,
                        context_injected=bool(worker_context),
                    )
                worker_outputs.append(worker_output)
                yield self._sse_event(
                    "team_worker_done" if worker_output.get("status") == "completed" else "team_worker_failed",
                    {
                        "run_id": stream_run_id,
                        **worker_output,
                    },
                )

        worker_order = {agent_id: index for index, agent_id in enumerate(route.worker_agent_ids)}
        worker_outputs.sort(key=lambda item: worker_order.get(str(item.get("agent_id")), len(worker_order)))
        completed_worker_outputs = [item for item in worker_outputs if item.get("status") == "completed"]
        approval_worker_outputs = [item for item in worker_outputs if item.get("status") == "approval_required"]
        if approval_worker_outputs:
            approval_response = await self._build_parallel_team_approval_response(
                team_run_id=stream_run_id,
                request_context=request_context,
                session_id=session_id,
                route_run_config=route_run_config,
                route=route,
                message=message,
                worker_outputs=worker_outputs,
                approval_worker_output=approval_worker_outputs[0],
                history_loaded=history_loaded,
                started_at=started_at,
                model=route_run_config.model_name,
                run_kind="stream",
                mcp_server_ids=mcp_server_ids,
                skill_ids=skill_ids,
                skill_tags=skill_tags,
                model_name=model_name,
            )
            yield self._sse_event("approval_pending", approval_response.model_dump(mode="json"))
            yield self._sse_event("approval_required", approval_response.model_dump(mode="json"))
            return

        if not completed_worker_outputs:
            failed_agents = ", ".join(str(item.get("agent_id")) for item in worker_outputs)
            await self._save_team_run_trace(
                team_run_id=stream_run_id,
                run_kind="stream",
                request_context=request_context,
                session_id=session_id,
                route_run_config=route_run_config,
                aggregator_agent_id=route.aggregator_agent_id,
                model=route_run_config.model_name,
                status="failed",
                final_message=None,
                usage=None,
                worker_outputs=worker_outputs,
                started_at=started_at,
                error=f"并行协同所有 worker 均失败: {failed_agents}",
            )
            yield self._sse_event(
                "error",
                self._build_stream_error_payload(
                    error=AIConfigValidationError(f"并行协同所有 worker 均失败: {failed_agents}"),
                    request_id=request_context.request_id,
                    session_id=session_id,
                    agent_id=route.selected_agent_id,
                    model=route_run_config.model_name,
                    history_loaded=history_loaded,
                    stream_mode="fallback",
                    mcp_servers=[],
                    skills=[],
                    run_config=route_run_config,
                ),
            )
            return

        yield self._sse_event(
            "aggregate_start",
            {
                "run_id": stream_run_id,
                "agent_id": route.aggregator_agent_id,
                "role": self._parallel_agent_role(route.aggregator_agent_id),
            },
        )
        try:
            aggregator_output = await self._run_parallel_aggregator(
                aggregator_agent_id=route.aggregator_agent_id,
                request_context=request_context,
                message=message,
                worker_outputs=worker_outputs,
                model_name=model_name,
                message_history=previous_history_messages,
            )
        except Exception as exc:
            await self._save_team_run_trace(
                team_run_id=stream_run_id,
                run_kind="stream",
                request_context=request_context,
                session_id=session_id,
                route_run_config=route_run_config,
                aggregator_agent_id=route.aggregator_agent_id,
                model=model_name or route_run_config.model_name,
                status="failed",
                final_message=None,
                usage=None,
                worker_outputs=worker_outputs,
                started_at=started_at,
                error=str(exc),
            )
            yield self._sse_event(
                "error",
                self._build_stream_error_payload(
                    error=exc,
                    request_id=request_context.request_id,
                    session_id=session_id,
                    agent_id=route.aggregator_agent_id,
                    model=model_name or route_run_config.model_name,
                    history_loaded=history_loaded,
                    stream_mode="fallback",
                    mcp_servers=[],
                    skills=[],
                    run_config=route_run_config,
                ),
            )
            return

        final_message = aggregator_output["message"]
        yield self._sse_event("delta", {"run_id": stream_run_id, "text": final_message})
        history_messages = await self._build_parallel_team_history_messages(
            previous_messages=previous_history_messages,
            message=message,
            final_message=final_message,
        )
        history_saved = await self._save_parallel_team_history(
            session_id=session_id,
            request_context=request_context,
            messages=history_messages,
            model=aggregator_output["model"],
            team_results=worker_outputs,
            aggregator_agent_id=route.aggregator_agent_id,
            route_reason=route.reason,
        )
        response = AgentChatResponse(
            run_id=stream_run_id,
            agent_id=route.selected_agent_id,
            model=aggregator_output["model"],
            status="completed",
            message=final_message,
            request_id=request_context.request_id,
            session_id=session_id,
            usage=aggregator_output["usage"],
            meta=self._build_run_meta(
                run_kind="stream",
                stream_mode="fallback",
                history_loaded=history_loaded,
                history_saved=history_saved,
                message_count=len(history_messages),
                mcp_servers=[],
                skills=[],
                run_config=route_run_config,
                team_results=worker_outputs,
            ),
        )
        await self._save_team_run_trace(
            team_run_id=stream_run_id,
            run_kind="stream",
            request_context=request_context,
            session_id=session_id,
            route_run_config=route_run_config,
            aggregator_agent_id=route.aggregator_agent_id,
            model=aggregator_output["model"],
            status=response.status,
            final_message=response.message,
            usage=response.usage,
            worker_outputs=worker_outputs,
            started_at=started_at,
        )
        yield self._sse_event("done", response.model_dump(mode="json"))

    async def _build_parallel_team_approval_response(
            self,
            *,
            team_run_id: str,
            request_context: RequestContext,
            session_id: str | None,
            route_run_config: ResolvedRunConfig,
            route: ResolvedAgentRoute,
            message: str,
            worker_outputs: list[dict[str, Any]],
            approval_worker_output: dict[str, Any],
            history_loaded: bool,
            started_at: float,
            model: str,
            run_kind: str,
            mcp_server_ids: list[str] | None,
            skill_ids: list[str] | None,
            skill_tags: list[str] | None,
            model_name: str | None,
    ) -> AgentChatResponse:
        deferred_payload = AgentDeferredToolRequestsPayload.model_validate(
            approval_worker_output.get("deferred_tool_requests") or {}
        )
        response = AgentChatResponse(
            run_id=team_run_id,
            agent_id=route.selected_agent_id,
            model=model,
            status="approval_required",
            message=None,
            deferred_tool_requests=deferred_payload,
            request_id=request_context.request_id,
            session_id=session_id,
            usage=None,
            meta=self._build_run_meta(
                run_kind=run_kind,
                stream_mode="fallback" if run_kind == "stream" else None,
                history_loaded=history_loaded,
                history_saved=False,
                message_count=0,
                mcp_servers=[],
                skills=[],
                run_config=route_run_config,
                team_results=worker_outputs,
            ),
        )
        await self._attach_team_worker_approval_record(
            response=response,
            request_context=request_context,
            worker_output=approval_worker_output,
            route=route,
            original_message=message,
            worker_outputs=worker_outputs,
            mcp_server_ids=mcp_server_ids,
            skill_ids=skill_ids,
            skill_tags=skill_tags,
            model_name=model_name,
        )
        await self._save_team_run_trace(
            team_run_id=team_run_id,
            run_kind=run_kind,
            request_context=request_context,
            session_id=session_id,
            route_run_config=route_run_config,
            aggregator_agent_id=route.aggregator_agent_id or "",
            model=model,
            status=response.status,
            final_message=None,
            usage=None,
            worker_outputs=worker_outputs,
            started_at=started_at,
            error="等待 Team Worker 审批",
        )
        return response

    async def _resume_parallel_team_worker(
            self,
            *,
            request_context: RequestContext,
            approval_record: ApprovalRecord,
            approvals: list[AgentApprovalDecision],
            session_id: str | None,
            model_name: str | None,
    ) -> AgentChatResponse:
        started_at = time.perf_counter()
        metadata = approval_record.metadata
        team_run_id = str(metadata.get("team_run_id") or approval_record.run_id)
        worker_agent_id = str(metadata.get("worker_agent_id") or approval_record.agent_id)
        aggregator_agent_id = str(metadata.get("aggregator_agent_id") or "summary-agent")
        original_message = str(metadata.get("original_message") or self._extract_latest_user_message(
            ModelMessagesTypeAdapter.validate_json(approval_record.message_history_json)
        ) or "")
        worker_outputs = list(metadata.get("worker_outputs") or [])
        route = dict(metadata.get("route") or {})
        resumed_session_id = session_id or approval_record.session_id
        requested_model_name = model_name or metadata.get("model_name")

        run_config = await self._resolve_run_config(
            agent_id=worker_agent_id,
            model_name=requested_model_name if isinstance(requested_model_name, str) else None,
            mcp_server_ids=list(metadata.get("mcp_server_ids") or []),
            skill_ids=list(metadata.get("skill_ids") or []),
            route_message=original_message,
            allow_agent_routing=False,
        )
        resolved_agent_id, resolved_model, agent = self._resolve_agent(run_config)
        skill_resolution = self._resolve_skills(
            agent_id=resolved_agent_id,
            message=original_message,
            skill_ids=list(run_config.skill_ids),
            skill_tags=list(metadata.get("skill_tags") or []),
        )
        deps = self._build_deps(request_context, resolved_skill_names=tuple(skill_resolution.skill_names))
        resolved_mcp_server_ids, run_toolsets = self._resolve_request_toolsets(
            mcp_server_ids=run_config.mcp_server_keys,
            mcp_server_configs=run_config.mcp_servers if run_config.source == "database" else (),
            route_message=original_message,
            skill_resolution=skill_resolution,
            allow_auto_route=run_config.source != "database",
        )
        deferred_tool_results = self._build_deferred_tool_results(approvals)
        message_history = ModelMessagesTypeAdapter.validate_json(approval_record.message_history_json)

        try:
            result = await agent.run(
                deps=deps,
                message_history=message_history,
                deferred_tool_results=deferred_tool_results,
                instructions=skill_resolution.instructions or None,
                toolsets=run_toolsets or None,
            )
        except AIRuntimeError:
            raise
        except Exception as exc:
            raise AIRunExecutionError("team worker resume run 执行失败") from exc

        output = result.output
        if isinstance(output, DeferredToolRequests):
            deferred_payload = self._serialize_deferred_tool_requests(result, output)
            resumed_worker_output = self._build_parallel_worker_result(
                agent_id=resolved_agent_id,
                model=resolved_model,
                status="approval_required",
                message=None,
                usage=self._serialize_usage(result),
                mcp_servers=resolved_mcp_server_ids,
                skills=skill_resolution.skill_names,
                started_at=started_at,
                deferred_tool_requests=deferred_payload.model_dump(mode="json"),
                context_injected=bool(_find_worker_output(worker_outputs, resolved_agent_id).get("context_injected")),
            )
            updated_worker_outputs = _replace_worker_output(worker_outputs, resumed_worker_output)
            response = AgentChatResponse(
                run_id=team_run_id,
                agent_id=str(route.get("selected_agent_id") or "team:auto-parallel"),
                model=resolved_model,
                status="approval_required",
                message=None,
                deferred_tool_requests=deferred_payload,
                request_id=request_context.request_id,
                session_id=resumed_session_id,
                usage=self._serialize_usage(result),
                meta=self._build_team_resume_meta(
                    route=route,
                    run_kind="resume",
                    history_loaded=True,
                    history_saved=False,
                    team_results=updated_worker_outputs,
                    model_key=run_config.model_key,
                    provider_key=run_config.model.provider_key,
                    config_source=run_config.source,
                    config_version=run_config.config_version,
                ),
            )
            await self._attach_team_worker_approval_record(
                response=response,
                request_context=request_context,
                worker_output=resumed_worker_output,
                route_dict=route,
                original_message=original_message,
                worker_outputs=updated_worker_outputs,
                mcp_server_ids=list(metadata.get("mcp_server_ids") or []),
                skill_ids=list(metadata.get("skill_ids") or []),
                skill_tags=list(metadata.get("skill_tags") or []),
                model_name=requested_model_name if isinstance(requested_model_name, str) else None,
            )
            await self.approval_store.mark_completed(approval_record.approval_id)
            await self._save_team_run_trace_from_route(
                team_run_id=team_run_id,
                run_kind="resume",
                request_context=request_context,
                session_id=resumed_session_id,
                route=route,
                aggregator_agent_id=aggregator_agent_id,
                model=resolved_model,
                status=response.status,
                final_message=None,
                usage=response.usage,
                worker_outputs=updated_worker_outputs,
                started_at=started_at,
                error="等待 Team Worker 审批",
            )
            return response

        resumed_worker_output = self._build_parallel_worker_result(
            agent_id=resolved_agent_id,
            model=resolved_model,
            status="completed",
            message=str(output),
            usage=self._serialize_usage(result),
            mcp_servers=resolved_mcp_server_ids,
            skills=skill_resolution.skill_names,
            started_at=started_at,
            context_injected=bool(_find_worker_output(worker_outputs, resolved_agent_id).get("context_injected")),
        )
        updated_worker_outputs = _replace_worker_output(worker_outputs, resumed_worker_output)
        previous_history_messages = await self.history_store.load_messages(
            resumed_session_id,
            request_context=request_context,
            agent_id="team:auto-parallel",
        )
        aggregator_output = await self._run_parallel_aggregator(
            aggregator_agent_id=aggregator_agent_id,
            request_context=request_context,
            message=original_message,
            worker_outputs=updated_worker_outputs,
            model_name=model_name,
            message_history=previous_history_messages,
        )
        history_messages = await self._build_parallel_team_history_messages(
            previous_messages=previous_history_messages,
            message=original_message,
            final_message=aggregator_output["message"],
        )
        history_saved = await self._save_parallel_team_history(
            session_id=resumed_session_id,
            request_context=request_context,
            messages=history_messages,
            model=aggregator_output["model"],
            team_results=updated_worker_outputs,
            aggregator_agent_id=aggregator_agent_id,
            route_reason=str(route.get("reason") or "Team worker 审批续跑后重新聚合"),
        )
        await self.approval_store.mark_completed(approval_record.approval_id)
        response = AgentChatResponse(
            run_id=team_run_id,
            agent_id=str(route.get("selected_agent_id") or "team:auto-parallel"),
            model=aggregator_output["model"],
            status="completed",
            message=aggregator_output["message"],
            request_id=request_context.request_id,
            session_id=resumed_session_id,
            usage=aggregator_output["usage"],
            meta=self._build_team_resume_meta(
                route=route,
                run_kind="resume",
                history_loaded=True,
                history_saved=history_saved,
                message_count=len(history_messages),
                team_results=updated_worker_outputs,
                model_key=run_config.model_key,
                provider_key=run_config.model.provider_key,
                config_source=run_config.source,
                config_version=run_config.config_version,
            ),
        )
        await self._save_team_run_trace_from_route(
            team_run_id=team_run_id,
            run_kind="resume",
            request_context=request_context,
            session_id=resumed_session_id,
            route=route,
            aggregator_agent_id=aggregator_agent_id,
            model=aggregator_output["model"],
            status=response.status,
            final_message=response.message,
            usage=response.usage,
            worker_outputs=updated_worker_outputs,
            started_at=started_at,
        )
        return response

    async def _run_parallel_worker(
            self,
            *,
            agent_id: str,
            request_context: RequestContext,
            message: str,
            session_id: str | None,
            model_name: str | None,
            mcp_server_ids: list[str] | None,
            skill_ids: list[str] | None,
            skill_tags: list[str] | None,
            team_context: str | None,
    ) -> dict[str, Any]:
        started_at = time.perf_counter()
        resolved_agent_id = agent_id
        resolved_model = model_name or ""
        skill_names: list[str] = []
        resolved_mcp_server_ids: list[str] = []
        try:
            run_config = await self._resolve_run_config(
                agent_id=agent_id,
                model_name=model_name,
                mcp_server_ids=mcp_server_ids,
                skill_ids=skill_ids,
                route_message=message,
                allow_agent_routing=False,
            )
            resolved_agent_id, resolved_model, agent = self._resolve_agent(run_config)
            skill_resolution = self._resolve_skills(
                agent_id=resolved_agent_id,
                message=message,
                skill_ids=list(run_config.skill_ids),
                skill_tags=skill_tags,
            )
            skill_names = skill_resolution.skill_names
            deps = self._build_deps(request_context, resolved_skill_names=tuple(skill_names))
            resolved_mcp_server_ids, run_toolsets = self._resolve_request_toolsets(
                mcp_server_ids=run_config.mcp_server_keys,
                mcp_server_configs=run_config.mcp_servers if run_config.source == "database" else (),
                route_message=message,
                skill_resolution=skill_resolution,
                allow_auto_route=run_config.source != "database",
            )
            message_history = await self.history_store.load_messages(
                session_id,
                request_context=request_context,
                agent_id=resolved_agent_id,
            )
            worker_message = self._build_parallel_worker_message(
                original_message=message,
                team_context=team_context,
            )
            await self._record_tool_exposure(
                agent_id=resolved_agent_id,
                request_id=request_context.request_id,
                message=worker_message,
                agent=agent,
                deps=deps,
                additional_toolsets=run_toolsets,
                resolved_mcp_server_ids=resolved_mcp_server_ids,
            )
            result = await agent.run(
                worker_message,
                deps=deps,
                message_history=message_history or None,
                instructions=skill_resolution.instructions or None,
                toolsets=run_toolsets or None,
            )
        except Exception as exc:
            return self._build_parallel_worker_result(
                agent_id=resolved_agent_id,
                model=resolved_model,
                status="failed",
                message=None,
                usage=None,
                mcp_servers=resolved_mcp_server_ids,
                skills=skill_names,
                started_at=started_at,
                error=str(exc),
                error_type=type(exc).__name__,
                context_injected=bool(team_context),
            )

        output = result.output
        if isinstance(output, DeferredToolRequests):
            deferred_payload = self._serialize_deferred_tool_requests(result, output)
            return self._build_parallel_worker_result(
                agent_id=resolved_agent_id,
                model=resolved_model,
                status="approval_required",
                message=None,
                usage=self._serialize_usage(result),
                mcp_servers=resolved_mcp_server_ids,
                skills=skill_names,
                started_at=started_at,
                deferred_tool_requests=deferred_payload.model_dump(mode="json"),
                context_injected=bool(team_context),
            )
        return self._build_parallel_worker_result(
            agent_id=resolved_agent_id,
            model=resolved_model,
            status="completed",
            message=str(output),
            usage=self._serialize_usage(result),
            mcp_servers=resolved_mcp_server_ids,
            skills=skill_names,
            started_at=started_at,
            context_injected=bool(team_context),
        )

    async def _run_parallel_aggregator(
            self,
            *,
            aggregator_agent_id: str,
            request_context: RequestContext,
            message: str,
            worker_outputs: list[dict[str, Any]],
            model_name: str | None,
            message_history: list[Any],
    ) -> dict[str, Any]:
        run_config = await self._resolve_run_config(
            agent_id=aggregator_agent_id,
            model_name=model_name,
            mcp_server_ids=[],
            skill_ids=[],
            route_message=message,
            allow_agent_routing=False,
        )
        resolved_agent_id, resolved_model, agent = self._resolve_agent(run_config)
        deps = self._build_deps(request_context)
        aggregate_prompt = self._build_parallel_aggregate_prompt(
            original_message=message,
            worker_outputs=worker_outputs,
        )
        try:
            result = await agent.run(
                aggregate_prompt,
                deps=deps,
                message_history=message_history or None,
            )
        except AIRuntimeError:
            raise
        except Exception as exc:
            raise AIRunExecutionError("parallel aggregator 执行失败") from exc

        output = result.output
        if isinstance(output, DeferredToolRequests):
            raise AIConfigValidationError(f"并行协同暂不支持 aggregator 进入审批: {resolved_agent_id}")
        return {
            "agent_id": resolved_agent_id,
            "model": resolved_model,
            "message": str(output),
            "usage": self._serialize_usage(result),
        }

    @staticmethod
    def _build_parallel_aggregate_prompt(*, original_message: str, worker_outputs: list[dict[str, Any]]) -> str:
        completed_outputs = [item for item in worker_outputs if item.get("status") == "completed"]
        failed_outputs = [item for item in worker_outputs if item.get("status") == "failed"]
        completed_sections = "\n\n".join(
            f"## {item['agent_id']} ({item.get('role')})\n{item['message']}" for item in completed_outputs
        )
        failed_sections = "\n".join(
            f"- {item['agent_id']}: {item.get('error_type')} - {item.get('error')}" for item in failed_outputs
        )
        return (
            "请整合多个 Agent 的并行结果，输出精简、去重、可执行的最终答复。\n"
            "不要复述 worker 原文；只保留必要结论。默认使用以下结构：\n"
            "1. 结论\n"
            "2. 推荐方案\n"
            "3. 风险 Top 5\n"
            "4. 下一步行动\n"
            "如果有失败的 Agent，请在最后补充“协同失败项”。\n\n"
            f"# 用户原始请求\n{original_message}\n\n"
            f"# 成功的子 Agent 结果\n{completed_sections or '无'}\n\n"
            f"# 失败的子 Agent\n{failed_sections or '无'}"
        )

    def _build_parallel_worker_result(
            self,
            *,
            agent_id: str,
            model: str,
            status: str,
            message: str | None,
            usage: dict[str, Any] | None,
            mcp_servers: list[str],
            skills: list[str],
            started_at: float,
            error: str | None = None,
            error_type: str | None = None,
            deferred_tool_requests: dict[str, Any] | None = None,
            context_injected: bool = False,
    ) -> dict[str, Any]:
        return {
            "agent_id": agent_id,
            "role": self._parallel_agent_role(agent_id),
            "status": status,
            "model": model,
            "message": message,
            "error": error,
            "error_type": error_type,
            "deferred_tool_requests": deferred_tool_requests,
            "duration_ms": round((time.perf_counter() - started_at) * 1000, 2),
            "context_injected": context_injected,
            "usage": usage,
            "mcp_servers": mcp_servers,
            "skills": skills,
        }

    @staticmethod
    def _parallel_agent_role(agent_id: str) -> str:
        return {
            "explore-agent": "explore",
            "planner-agent": "plan",
            "review-agent": "review",
            "summary-agent": "summary",
        }.get(agent_id, agent_id.removesuffix("-agent"))

    @staticmethod
    def _build_parallel_worker_message(*, original_message: str, team_context: str | None) -> str:
        if not team_context:
            return original_message
        return (
            "# 当前用户请求\n"
            f"{original_message}\n\n"
            "# 上一轮团队汇总\n"
            f"{team_context}\n\n"
            "请基于上一轮团队汇总继续完成你当前角色的分析；不要声称看不到历史。"
        )

    @staticmethod
    def _extract_latest_parallel_team_summary(messages: list[Any], max_chars: int = 2000) -> str | None:
        for message in reversed(messages):
            if not isinstance(message, ModelResponse):
                continue
            text_parts = [
                part.content
                for part in message.parts
                if isinstance(part, TextPart) and isinstance(part.content, str)
            ]
            summary = "\n".join(text_parts).strip()
            if not summary:
                continue
            if len(summary) <= max_chars:
                return summary
            return summary[-max_chars:]
        return None

    async def _build_parallel_team_history_messages(
            self,
            *,
            previous_messages: list[Any],
            message: str,
            final_message: str,
    ) -> list[Any]:
        return [
            *previous_messages,
            ModelRequest(parts=[UserPromptPart(content=message)]),
            ModelResponse(parts=[TextPart(content=final_message)]),
        ]

    async def _save_parallel_team_history(
            self,
            *,
            session_id: str | None,
            request_context: RequestContext,
            messages: list[Any],
            model: str,
            team_results: list[dict[str, Any]],
            aggregator_agent_id: str,
            route_reason: str,
    ) -> bool:
        if not session_id:
            return False
        await self.history_store.save_messages(
            session_id,
            messages,
            request_context=request_context,
            agent_id="team:auto-parallel",
            model=model,
            skills=[],
            mcp_servers=[],
            usage={
                "team": {
                    "aggregator_agent_id": aggregator_agent_id,
                    "route_reason": route_reason,
                    "worker_agent_ids": [item["agent_id"] for item in team_results],
                    "completed_worker_agent_ids": [
                        item["agent_id"] for item in team_results if item.get("status") == "completed"
                    ],
                    "failed_worker_agent_ids": [
                        item["agent_id"] for item in team_results if item.get("status") == "failed"
                    ],
                },
                "team_results": [
                    {
                        "agent_id": item["agent_id"],
                        "role": item["role"],
                        "status": item["status"],
                        "model": item["model"],
                        "usage": item["usage"],
                        "mcp_servers": item["mcp_servers"],
                        "skills": item["skills"],
                        "duration_ms": item["duration_ms"],
                        "context_injected": item["context_injected"],
                        "error": item["error"],
                        "error_type": item["error_type"],
                    }
                    for item in team_results
                ],
            },
        )
        return True

    async def get_team_run_trace(self, team_run_id: str) -> dict[str, Any] | None:
        if self.team_trace_store is None:
            return None
        return await self.team_trace_store.get(team_run_id)

    async def _save_team_run_trace(
            self,
            *,
            team_run_id: str,
            run_kind: str,
            request_context: RequestContext,
            session_id: str | None,
            route_run_config: ResolvedRunConfig,
            aggregator_agent_id: str,
            model: str,
            status: str,
            final_message: str | None,
            usage: dict[str, Any] | None,
            worker_outputs: list[dict[str, Any]],
            started_at: float,
            error: str | None = None,
    ) -> None:
        if self.team_trace_store is None:
            return
        route = route_run_config.agent_route
        await self.team_trace_store.save(
            TeamRunTracePayload(
                team_run_id=team_run_id,
                run_kind=run_kind,
                agent_id=route.selected_agent_id if route is not None else "team:auto-parallel",
                request_id=request_context.request_id,
                session_id=session_id,
                user_id=request_context.user_id,
                tenant_id=request_context.tenant_id,
                status=status,
                model=model,
                aggregator_agent_id=aggregator_agent_id,
                worker_agent_ids=[str(item.get("agent_id")) for item in worker_outputs],
                route=self._build_agent_route_trace(route),
                usage=usage,
                final_message=final_message,
                duration_ms=round((time.perf_counter() - started_at) * 1000, 2),
                error=error,
                metadata={
                    "config_source": route_run_config.source,
                    "model_key": route_run_config.model_key,
                    "provider_key": route_run_config.model.provider_key,
                    "config_version": route_run_config.config_version,
                },
                worker_results=worker_outputs,
            )
        )

    async def _save_team_run_trace_from_route(
            self,
            *,
            team_run_id: str,
            run_kind: str,
            request_context: RequestContext,
            session_id: str | None,
            route: dict[str, Any],
            aggregator_agent_id: str,
            model: str,
            status: str,
            final_message: str | None,
            usage: dict[str, Any] | None,
            worker_outputs: list[dict[str, Any]],
            started_at: float,
            error: str | None = None,
    ) -> None:
        if self.team_trace_store is None:
            return
        await self.team_trace_store.save(
            TeamRunTracePayload(
                team_run_id=team_run_id,
                run_kind=run_kind,
                agent_id=str(route.get("selected_agent_id") or "team:auto-parallel"),
                request_id=request_context.request_id,
                session_id=session_id,
                user_id=request_context.user_id,
                tenant_id=request_context.tenant_id,
                status=status,
                model=model,
                aggregator_agent_id=aggregator_agent_id,
                worker_agent_ids=[str(item.get("agent_id")) for item in worker_outputs],
                route=route,
                usage=usage,
                final_message=final_message,
                duration_ms=round((time.perf_counter() - started_at) * 1000, 2),
                error=error,
                metadata={"resume": True},
                worker_results=worker_outputs,
            )
        )

    @staticmethod
    def _build_team_resume_meta(
            *,
            route: dict[str, Any],
            run_kind: str,
            history_loaded: bool,
            history_saved: bool,
            team_results: list[dict[str, Any]],
            model_key: str | None,
            provider_key: str | None,
            config_source: str | None,
            config_version: str | None,
            message_count: int = 0,
    ) -> AgentRunMeta:
        return AgentRunMeta(
            run_kind=run_kind,
            stream_mode=None,
            history_loaded=history_loaded,
            history_saved=history_saved,
            message_count=message_count,
            mcp_servers=[],
            skills=[],
            config_source=config_source,
            model_key=model_key,
            provider_key=provider_key,
            config_version=config_version,
            agent_route=route,
            team_results=team_results,
        )

    @staticmethod
    def _build_agent_route_trace(route: ResolvedAgentRoute | None) -> dict[str, Any]:
        if route is None:
            return {}
        return {
            "requested_agent_id": route.requested_agent_id,
            "selected_agent_id": route.selected_agent_id,
            "source": route.source,
            "reason": route.reason,
            "matched_keywords": list(route.matched_keywords),
            "mode": route.mode,
            "candidate_agent_ids": list(route.candidate_agent_ids),
            "worker_agent_ids": list(route.worker_agent_ids),
            "aggregator_agent_id": route.aggregator_agent_id,
        }

    def _build_runtime_model(self, run_config: ResolvedRunConfig) -> Any | None:
        """把控制面 provider 配置转换成 PydanticAI 可直接消费的模型对象。"""

        provider = run_config.provider
        if provider is None:
            return None

        if provider.provider_type in {"openai", "openai_compatible"}:
            return OpenAIChatModel(
                model_name=run_config.model_name,
                provider=OpenAIProvider(
                    api_key=decrypt_secret(provider.api_key_encrypted) or self.settings.openai_api_key,
                    base_url=provider.base_url or self.settings.openai_base_url,
                ),
            )

        raise AIConfigValidationError(f"暂不支持的模型供应商类型: {provider.provider_type}")

    @staticmethod
    def _build_model_cache_key(run_config: ResolvedRunConfig) -> str:
        if run_config.config_version:
            return f"{run_config.model_key}:{run_config.config_version}"
        return run_config.model_key

    def _build_deps(
            self,
            request_context: RequestContext,
            *,
            resolved_skill_names: tuple[str, ...] = (),
    ) -> AgentDeps:
        # `AgentDeps` 是本次运行注入给工具层和动态 instructions 的运行时依赖集合。
        # 当前先放 request/settings/db/redis/http_client/tool_audit/mcp_manager/skill_registry，
        # 后续继续扩 history / approval / skill 也会沿着这个入口演进。
        return AgentDeps(
            request=request_context,
            settings=self.settings,
            db_session_factory=AsyncSessionLocal,
            redis=redis_client.redis_pool,
            http_client=self.http_client,
            tool_audit=self.tool_audit,
            mcp_manager=self.mcp_manager,
            skill_registry=self.skill_registry,
            resolved_skill_names=resolved_skill_names,
        )

    def _resolve_skills(
            self,
            *,
            agent_id: str,
            message: str | None,
            skill_ids: list[str] | None,
            skill_tags: list[str] | None,
    ) -> SkillResolution:
        if self.skill_resolver is None:
            return SkillResolution(
                skills=(),
                instructions=(),
                required_toolset_ids=(),
                required_mcp_server_ids=(),
            )

        return self.skill_resolver.resolve(
            agent_id=agent_id,
            message=message,
            skill_ids=skill_ids,
            skill_tags=skill_tags,
        )

    def _resolve_request_toolsets(
            self,
            *,
            mcp_server_ids: list[str] | None,
            mcp_server_configs: tuple[ResolvedMCPServerConfig, ...] = (),
            route_message: str | None,
            skill_resolution: SkillResolution,
            allow_auto_route: bool = True,
    ) -> tuple[list[str], list[AbstractToolset[AgentDeps]]]:
        """解析请求级动态能力并转换成本轮附加 toolsets。

        规则是：
        - skills 先声明本轮额外需要的 toolsets / MCP
        - 未显式传入 `mcp_servers` 时，允许 MCP manager 根据消息内容做自动路由
        - 数据库控制面启用时，MCP 选择必须来自 resolver，避免绕过绑定校验
        """

        skill_toolsets = build_registered_toolsets(list(skill_resolution.required_toolset_ids))
        wrapped_skill_toolsets = wrap_toolsets_with_audit(
            wrap_toolsets_with_metadata_approval(skill_toolsets)
        )

        db_mcp_server_ids = [config.server_key for config in mcp_server_configs]
        skill_mcp_server_ids = [] if mcp_server_configs else skill_resolution.required_mcp_server_ids
        explicit_mcp_server_ids = _dedupe_server_ids(
            [*(db_mcp_server_ids or mcp_server_ids or []), *skill_mcp_server_ids]
        )

        if self.mcp_manager is None:
            return explicit_mcp_server_ids, wrapped_skill_toolsets

        auto_routed_server_ids = self.mcp_manager.resolve_server_ids(
            requested_server_ids=None,
            message=route_message,
        ) if allow_auto_route and not mcp_server_ids else []

        resolved_mcp_server_ids = _dedupe_server_ids([*explicit_mcp_server_ids, *auto_routed_server_ids])
        if not resolved_mcp_server_ids:
            return resolved_mcp_server_ids, wrapped_skill_toolsets

        if mcp_server_configs:
            managed_configs = [self._build_managed_mcp_config(config) for config in mcp_server_configs]
            return (
                resolved_mcp_server_ids,
                [*wrapped_skill_toolsets, *self.mcp_manager.build_toolsets_from_configs(managed_configs)],
            )

        return (
            resolved_mcp_server_ids,
            [*wrapped_skill_toolsets, *self.mcp_manager.build_toolsets(resolved_mcp_server_ids)],
        )

    @staticmethod
    def _build_managed_mcp_config(config: ResolvedMCPServerConfig) -> ManagedMCPServerConfig:
        common = {
            "id": config.server_key,
            "enabled": True,
            "auto_route_enabled": config.auto_route_enabled and config.allow_auto_route,
            "route_keywords": list(config.route_keywords),
            "tool_prefix": config.tool_prefix,
            "timeout": config.timeout_seconds or 5.0,
            "read_timeout": config.read_timeout_seconds or 300.0,
            "max_retries": config.max_retries if config.max_retries is not None else 1,
            "include_instructions": config.include_instructions,
        }
        headers = dict(config.headers or {})

        if config.transport == "stdio":
            if not config.command:
                raise MCPConfigurationError(f"MCP server 缺少 command: {config.server_key}")
            return ManagedMCPServerStdioConfig(
                **common,
                command=config.command,
                args=list(config.args),
                env=dict(config.env or {}) or None,
                cwd=config.cwd,
            )

        if config.transport == "sse":
            if not config.url:
                raise MCPConfigurationError(f"MCP server 缺少 url: {config.server_key}")
            return ManagedMCPServerSSEConfig(**common, url=config.url, headers=headers or None)

        if config.transport == "streamable-http":
            if not config.url:
                raise MCPConfigurationError(f"MCP server 缺少 url: {config.server_key}")
            return ManagedMCPServerStreamableHTTPConfig(**common, url=config.url, headers=headers or None)

        raise MCPConfigurationError(f"不支持的 MCP transport: {config.transport}")

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
            mcp_servers: list[str],
            skills: list[str],
            run_config: ResolvedRunConfig | None = None,
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
                mcp_servers=mcp_servers,
                skills=skills,
                run_config=run_config,
            ),
        )

        if isinstance(output, DeferredToolRequests):
            # 这里是 `/chat`、`/resume`、`/stream done` 共用的统一判定：
            # 只要输出不是最终文本，而是 deferred requests，就说明本轮进入审批/外部执行分支。
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
            mcp_servers: list[str],
            run_toolsets: list[AbstractToolset[AgentDeps]],
            instructions: tuple[str, ...],
            skills: list[str],
            run_config: ResolvedRunConfig | None = None,
            user_id: str | None = None,
            tenant_id: str | None = None,
    ) -> AsyncIterator[str]:
        """当模型不支持真正的 streamed request 时，退化成单次 run 再包装成 SSE。"""

        try:
            result = await agent.run(
                message,
                deps=deps,
                message_history=message_history or None,
                instructions=instructions or None,
                toolsets=run_toolsets or None,
            )
            fallback_request_context = RequestContext(
                request_id=request_id,
                user_id=user_id,
                tenant_id=tenant_id,
                session_id=session_id,
            )
            history_saved = await self._save_history(
                session_id=session_id,
                result=result,
                request_context=fallback_request_context,
                agent_id=agent_id,
                model=model,
                mcp_servers=mcp_servers,
                skills=skills,
            )
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
                    mcp_servers=mcp_servers,
                    skills=skills,
                    run_config=run_config,
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
            mcp_servers=mcp_servers,
            skills=skills,
            run_config=run_config,
        )
        response.meta.stream_mode = stream_mode
        await self._attach_approval_record(
            response,
            RequestContext(request_id=request_id, user_id=user_id, tenant_id=tenant_id, session_id=session_id),
        )
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
                    mcp_servers=mcp_servers,
                    skills=skills,
                    run_config=run_config,
                ).model_dump(mode="json"),
            },
        )
        if response.status == "approval_required":
            payload = response.model_dump(mode="json")
            # fallback 路径虽然拿不到细粒度 tool 事件，
            # 但审批语义仍然要与 native path 保持一致。
            yield self._sse_event("approval_pending", payload)
            yield self._sse_event("approval_required", payload)
            return

        yield self._sse_event("done", response.model_dump(mode="json"))

    async def _save_history(
            self,
            *,
            session_id: str | None,
            result: Any,
            request_context: RequestContext,
            agent_id: str,
            model: str,
            mcp_servers: list[str],
            skills: list[str],
    ) -> bool:
        """把本轮 run 结束后的完整消息历史写回会话存储。"""

        if not session_id:
            return False
        await self.history_store.save_messages(
            session_id,
            result.all_messages(),
            request_context=request_context,
            agent_id=agent_id,
            model=model,
            mcp_servers=mcp_servers,
            skills=skills,
            usage=self._serialize_usage(result),
        )
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

    async def _attach_approval_record(
        self,
        response: AgentChatResponse,
        request_context: RequestContext,
    ) -> None:
        payload = response.deferred_tool_requests
        if response.status != "approval_required" or payload is None:
            return

        record = await self.approval_store.create(
            run_id=response.run_id,
            agent_id=response.agent_id,
            request_id=request_context.request_id,
            session_id=response.session_id or request_context.session_id,
            user_id=request_context.user_id,
            message_history_json=payload.message_history_json,
            approval_tool_call_ids=[item.tool_call_id for item in payload.approvals],
            call_tool_call_ids=[item.tool_call_id for item in payload.calls],
            metadata={
                "model": response.model,
                "mcp_servers": response.meta.mcp_servers,
                "skills": response.meta.skills,
                "config_source": response.meta.config_source,
            },
        )
        payload.approval_id = record.approval_id
        payload.expires_at = record.expires_at
        payload.status = record.status

    async def _attach_team_worker_approval_record(
            self,
            *,
            response: AgentChatResponse,
            request_context: RequestContext,
            worker_output: dict[str, Any],
            original_message: str,
            worker_outputs: list[dict[str, Any]],
            mcp_server_ids: list[str] | None,
            skill_ids: list[str] | None,
            skill_tags: list[str] | None,
            model_name: str | None,
            route: ResolvedAgentRoute | None = None,
            route_dict: dict[str, Any] | None = None,
    ) -> None:
        payload = response.deferred_tool_requests
        if response.status != "approval_required" or payload is None:
            return
        route_payload = route_dict if route_dict is not None else self._build_agent_route_trace(route)
        worker_agent_id = str(worker_output.get("agent_id") or "")
        record = await self.approval_store.create(
            run_id=response.run_id,
            agent_id=worker_agent_id,
            request_id=request_context.request_id,
            session_id=response.session_id or request_context.session_id,
            user_id=request_context.user_id,
            message_history_json=payload.message_history_json,
            approval_tool_call_ids=[item.tool_call_id for item in payload.approvals],
            call_tool_call_ids=[item.tool_call_id for item in payload.calls],
            metadata={
                "kind": "team_worker_approval",
                "team_run_id": response.run_id,
                "team_agent_id": response.agent_id,
                "worker_agent_id": worker_agent_id,
                "aggregator_agent_id": route_payload.get("aggregator_agent_id"),
                "original_message": original_message,
                "route": route_payload,
                "worker_outputs": worker_outputs,
                "mcp_server_ids": list(mcp_server_ids or []),
                "skill_ids": list(skill_ids or []),
                "skill_tags": list(skill_tags or []),
                "model_name": model_name,
                "model": response.model,
            },
        )
        payload.approval_id = record.approval_id
        payload.expires_at = record.expires_at
        payload.status = record.status

    async def _load_approval_record_for_resume(
        self,
        *,
        approval_id: str | None,
        agent_id: str | None,
        session_id: str | None,
        user_id: str | None,
    ) -> ApprovalRecord | None:
        if not approval_id:
            return None
        record = await self.approval_store.get_pending(
            approval_id,
            agent_id=None,
            session_id=session_id,
            user_id=user_id,
        )
        if agent_id is None:
            return record
        if record.metadata.get("kind") == "team_worker_approval":
            allowed_agent_ids = {record.agent_id, record.metadata.get("team_agent_id")}
            if agent_id not in allowed_agent_ids:
                raise AIConfigValidationError("审批单 Agent 不匹配")
            return record
        if agent_id != record.agent_id:
            raise AIConfigValidationError("审批单 Agent 不匹配")
        return record

    @staticmethod
    def _validate_approval_decisions(
        *,
        approvals: list[AgentApprovalDecision],
        approval_record: ApprovalRecord | None,
    ) -> None:
        if approval_record is None:
            return
        valid_tool_call_ids = set(approval_record.approval_tool_call_ids)
        submitted_tool_call_ids = {item.tool_call_id for item in approvals}
        unknown_tool_call_ids = submitted_tool_call_ids - valid_tool_call_ids
        if unknown_tool_call_ids:
            raise AIConfigValidationError(
                f"审批结果包含未知 tool_call_id: {', '.join(sorted(unknown_tool_call_ids))}"
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
            additional_toolsets: list[AbstractToolset[AgentDeps]],
            resolved_mcp_server_ids: list[str],
    ) -> dict[str, dict[str, Any]]:
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
        toolset = agent._get_toolset(additional_toolsets=additional_toolsets)
        try:
            tools = await toolset.get_tools(run_context)
        except AIRuntimeError:
            raise
        except TimeoutError as exc:
            if resolved_mcp_server_ids:
                raise MCPRuntimeError(
                    f"MCP server 初始化超时: {', '.join(resolved_mcp_server_ids)}。"
                    "请检查服务可用性、访问令牌以及 timeout 配置。"
                ) from exc
            raise AIRunExecutionError("工具暴露信息收集超时") from exc
        except Exception as exc:
            if resolved_mcp_server_ids:
                raise MCPRuntimeError(
                    f"MCP server 初始化失败: {', '.join(resolved_mcp_server_ids)}。"
                    "请检查 MCP 命令、网络连接、鉴权参数或服务端日志。"
                ) from exc
            raise AIRunExecutionError("工具暴露信息收集失败") from exc
        tool_metadata = {name: dict(tool.tool_def.metadata or {}) for name, tool in tools.items()}
        self.tool_audit.record_tool_exposure(
            agent_id=agent_id,
            request_id=request_id,
            tool_names=list(tools.keys()),
            tool_metadata=tool_metadata,
        )
        return tool_metadata

    @staticmethod
    async def _iterate_stream_events(first_event: Any, stream: Any) -> AsyncIterator[Any]:
        # `anext(stream)` 已经消费掉首个事件，这里把它补回去，
        # 对后续处理方来说，就像是在遍历一条完整的事件流。
        yield first_event
        async for event in stream:
            yield event

    @staticmethod
    def _extract_stream_run_id_from_event(event: Any) -> str | None:
        # 目前只有最终的 AgentRunResultEvent 一定能稳定拿到 result.run_id，
        # 文本 part / tool 事件本身并不保证附带 run_id。
        if isinstance(event, AgentRunResultEvent):
            return AgentRunner._extract_run_id(event.result)
        return None

    def _build_stream_tool_call_payload(
            self,
            *,
            run_id: str,
            event: FunctionToolCallEvent,
            tool_metadata_by_name: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        # 这里返回的是前端协议层 payload，不直接暴露 PydanticAI 原始对象。
        return {
            "run_id": run_id,
            "tool_call_id": event.tool_call_id,
            "tool_name": event.part.tool_name,
            "args": self._normalize_tool_args(event.part.args),
            "args_valid": event.args_valid,
            "tool_metadata": dict(tool_metadata_by_name.get(event.part.tool_name, {})),
        }

    def _build_stream_tool_result_payload(
            self,
            *,
            run_id: str,
            event: FunctionToolResultEvent,
            tool_metadata_by_name: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        result = event.result
        tool_name = result.tool_name or ""
        if isinstance(result, RetryPromptPart):
            # RetryPromptPart 不是工具真正成功返回，而是“请模型修正后重试”的反馈。
            tool_result: Any = result.model_response()
            status = "retry"
        else:
            tool_result = self._normalize_value(result.content)
            status = result.outcome

        return {
            "run_id": run_id,
            "tool_call_id": result.tool_call_id,
            "tool_name": tool_name,
            "status": status,
            "result": tool_result,
            "tool_metadata": dict(tool_metadata_by_name.get(tool_name, {})),
        }

    @staticmethod
    def _normalize_tool_args(args: Any) -> dict[str, Any]:
        # PydanticAI 里的 tool args 可能是 dict，也可能是 JSON 字符串。
        # 这里统一整理成 dict，方便前端和测试断言。
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
    def _extract_latest_user_message(message_history: list[Any]) -> str | None:
        """从 message history 中提取最后一条用户消息，供 resume 自动路由使用。"""

        for message in reversed(message_history):
            parts = getattr(message, "parts", None)
            if not isinstance(parts, list):
                continue
            for part in reversed(parts):
                content = getattr(part, "content", None)
                if isinstance(content, str) and content.strip():
                    return content
        return None

    @staticmethod
    def _normalize_value(value: Any) -> Any:
        # 把工具返回值尽量标准化成稳定、可 JSON 化的结构，
        # 避免直接把复杂对象泄漏到 SSE payload 里。
        if value is None:
            return None
        if isinstance(value, str | int | float | bool):
            return value
        if isinstance(value, list):
            return [AgentRunner._normalize_value(item) for item in value]
        if isinstance(value, dict):
            return {str(key): AgentRunner._normalize_value(item) for key, item in value.items()}
        if hasattr(value, "model_dump"):
            return value.model_dump(mode="json")
        if hasattr(value, "__dict__"):
            return {str(key): AgentRunner._normalize_value(item) for key, item in vars(value).items()}
        return repr(value)

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
            mcp_servers: list[str],
            skills: list[str],
            run_config: ResolvedRunConfig | None = None,
            team_results: list[dict[str, Any]] | None = None,
    ) -> AgentRunMeta:
        return AgentRunMeta(
            run_kind=run_kind,
            stream_mode=stream_mode,
            history_loaded=history_loaded,
            history_saved=history_saved,
            message_count=message_count,
            mcp_servers=mcp_servers,
            skills=skills,
            config_source=run_config.source if run_config is not None else None,
            model_key=run_config.model_key if run_config is not None else None,
            provider_key=run_config.model.provider_key if run_config is not None else None,
            config_version=run_config.config_version if run_config is not None else None,
            agent_route=(
                {
                    "requested_agent_id": run_config.agent_route.requested_agent_id,
                    "selected_agent_id": run_config.agent_route.selected_agent_id,
                    "source": run_config.agent_route.source,
                    "reason": run_config.agent_route.reason,
                    "matched_keywords": list(run_config.agent_route.matched_keywords),
                    "mode": run_config.agent_route.mode,
                    "candidate_agent_ids": list(run_config.agent_route.candidate_agent_ids),
                    "worker_agent_ids": list(run_config.agent_route.worker_agent_ids),
                    "aggregator_agent_id": run_config.agent_route.aggregator_agent_id,
                }
                if run_config is not None and run_config.agent_route is not None
                else None
            ),
            team_results=team_results,
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
            mcp_servers: list[str],
            skills: list[str],
            run_config: ResolvedRunConfig | None = None,
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
                mcp_servers=mcp_servers,
                skills=skills,
                run_config=run_config,
            ).model_dump(mode="json"),
        }


def _dedupe_server_ids(server_ids: list[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for server_id in server_ids:
        normalized = server_id.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(normalized)
    return deduped


def _find_worker_output(worker_outputs: list[dict[str, Any]], agent_id: str) -> dict[str, Any]:
    for item in worker_outputs:
        if item.get("agent_id") == agent_id:
            return item
    return {}


def _replace_worker_output(worker_outputs: list[dict[str, Any]], replacement: dict[str, Any]) -> list[dict[str, Any]]:
    replaced = False
    updated: list[dict[str, Any]] = []
    replacement_agent_id = replacement.get("agent_id")
    for item in worker_outputs:
        if item.get("agent_id") == replacement_agent_id:
            updated.append(replacement)
            replaced = True
        else:
            updated.append(item)
    if not replaced:
        updated.append(replacement)
    return updated
