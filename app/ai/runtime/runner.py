import json
from collections.abc import AsyncIterator
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
    TextPart,
    TextPartDelta,
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
from app.ai.exceptions import AIDisabledError, AgentNotFoundError, AIRunExecutionError, AIRuntimeError, MCPRuntimeError
from app.ai.mcp import MCPManager
from app.ai.runtime.history import SessionHistoryStore
from app.ai.runtime.manager import AgentManager
from app.ai.runtime.resolved_config import ResolvedMCPServerConfig, ResolvedModelConfig, ResolvedRunConfig
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
            mcp_manager: MCPManager | None,
            skill_registry: SkillRegistry | None,
            skill_resolver: SkillResolver | None,
            enable_config_resolver: bool = False,
    ) -> None:
        self.settings = settings
        self.agent_manager = agent_manager
        self.http_client = http_client
        self.tool_audit = tool_audit
        self.history_store = history_store
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
            route_message=message,
            skill_resolution=skill_resolution,
            allow_auto_route=run_config.source != "database",
        )
        message_history = await self.history_store.load_messages(session_id)
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
            mcp_servers=resolved_mcp_server_ids,
            skills=skill_resolution.skill_names,
            run_config=run_config,
        )

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
            route_message=message,
            skill_resolution=skill_resolution,
            allow_auto_route=run_config.source != "database",
        )
        message_history = await self.history_store.load_messages(session_id)
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
                    history_saved = await self._save_history(session_id, result)
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
            message_history_json: str,
            approvals: list[AgentApprovalDecision],
            agent_id: str | None = None,
            session_id: str | None = None,
            model_name: str | None = None,
            mcp_server_ids: list[str] | None = None,
            skill_ids: list[str] | None = None,
            skill_tags: list[str] | None = None,
    ) -> AgentChatResponse:
        """基于上一轮 deferred tool requests 继续执行一次 run。

        当前 `resume` 仍采用无状态协议：
        - 前端回传 `message_history_json`
        - runner 负责把审批结果组装成 `DeferredToolResults`
        - run 完成后，如果带了 `session_id`，也会把新历史写回会话存储
        """

        run_config = await self._resolve_run_config(
            agent_id=agent_id,
            model_name=model_name,
            mcp_server_ids=mcp_server_ids,
            skill_ids=skill_ids,
        )
        resolved_agent_id, resolved_model, agent = self._resolve_agent(run_config)
        message_history = ModelMessagesTypeAdapter.validate_json(message_history_json)
        latest_user_message = self._extract_latest_user_message(message_history)
        skill_resolution = self._resolve_skills(
            agent_id=resolved_agent_id,
            message=latest_user_message,
            skill_ids=list(run_config.skill_ids),
            skill_tags=skill_tags,
        )
        deps = self._build_deps(request_context, resolved_skill_names=tuple(skill_resolution.skill_names))
        resolved_mcp_server_ids, run_toolsets = self._resolve_request_toolsets(
            mcp_server_ids=run_config.mcp_server_keys,
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
            mcp_servers=resolved_mcp_server_ids,
            skills=skill_resolution.skill_names,
            run_config=run_config,
        )

    async def _resolve_run_config(
            self,
            *,
            agent_id: str | None,
            model_name: str | None,
            mcp_server_ids: list[str] | None,
            skill_ids: list[str] | None,
    ) -> ResolvedRunConfig:
        """解析本轮 run 的控制面配置。

        未启用数据库控制面时，返回与旧逻辑一致的 settings fallback；
        启用后通过 AICapabilityResolver 校验模型与 MCP allowlist。
        """

        if not self.settings.enabled:
            raise AIDisabledError("AI 能力已关闭")

        if not self.enable_config_resolver:
            return self._build_settings_fallback_run_config(
                agent_id=agent_id,
                model_name=model_name,
                mcp_server_ids=mcp_server_ids,
                skill_ids=skill_ids,
            )

        async with AsyncSessionLocal() as session:
            resolver = AICapabilityResolver(
                settings=self.settings,
                repository=AIConfigRepository(session),
            )
            return await resolver.resolve(
                agent_id=agent_id,
                requested_model=model_name,
                requested_mcp_servers=mcp_server_ids,
                requested_skill_ids=skill_ids,
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

        raise AIRunExecutionError(f"暂不支持的模型供应商类型: {provider.provider_type}")

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

        explicit_mcp_server_ids = _dedupe_server_ids(
            [*(mcp_server_ids or []), *skill_resolution.required_mcp_server_ids]
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

        return (
            resolved_mcp_server_ids,
            [*wrapped_skill_toolsets, *self.mcp_manager.build_toolsets(resolved_mcp_server_ids)],
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

    async def _save_history(self, session_id: str | None, result: Any) -> bool:
        """把本轮 run 结束后的完整消息历史写回会话存储。"""

        if not session_id:
            return False
        await self.history_store.save_messages(session_id, result.all_messages())
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
