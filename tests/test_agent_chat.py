import asyncio
import json
import httpx
from fastapi.testclient import TestClient
from pydantic_ai import Agent
from pydantic_ai import RunContext
from pydantic_ai import models
from pydantic_ai.exceptions import ApprovalRequired
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.toolsets.function import FunctionToolset
from pydantic_ai.tools import DeferredToolRequests
from pydantic_ai.usage import RunUsage

from app.ai.config import AISettings
from app.ai.deps import AgentDeps, RequestContext
from app.main import app
from app.ai.schemas.agent import AgentManifest
from app.ai.toolsets.builtin import (
    get_builtin_request_toolset,
    get_builtin_runtime_toolset,
    get_builtin_time_toolset,
    get_builtin_toolsets,
)
from app.ai.toolsets import wrap_toolsets_with_audit, wrap_toolsets_with_metadata_approval
from app.ai.toolsets.approval import tool_requires_approval, wrap_toolset_with_metadata_approval
from app.ai.toolsets.conventions import create_function_toolset
from app.ai.toolsets.metadata import build_tool_metadata
from app.ai.services.tool_audit import ToolAuditService

models.ALLOW_MODEL_REQUESTS = False


def _build_test_deps(request_id: str) -> AgentDeps:
    return AgentDeps(
        request=RequestContext(request_id=request_id),
        settings=AISettings(),
        db_session_factory=None,
        redis=None,
        http_client=httpx.AsyncClient(),
        tool_audit=ToolAuditService(),
        mcp_manager=None,
        skill_registry=None,
        resolved_skill_names=(),
    )


def _parse_sse_events(raw: str) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    for block in raw.strip().split("\n\n"):
        if not block.strip():
            continue
        event_name = ""
        data_payload = ""
        for line in block.splitlines():
            if line.startswith("event: "):
                event_name = line.removeprefix("event: ").strip()
            elif line.startswith("data: "):
                data_payload = line.removeprefix("data: ").strip()
        if event_name:
            events.append((event_name, json.loads(data_payload or "{}")))
    return events


def _build_approval_demo_toolset() -> FunctionToolset[AgentDeps]:
    toolset: FunctionToolset[AgentDeps] = create_function_toolset(
        id="approval-demo-toolset",
        metadata={"toolset": {"id": "approval-demo-toolset", "kind": "business", "owner": "test"}},
    )

    @toolset.tool_plain(
        metadata=build_tool_metadata(category="ops", readonly=False, risk="high", approval_required=False),
    )
    def delete_demo_resource() -> str:
        """Delete a demo resource."""

        return "deleted"

    return toolset


def _build_approval_demo_agent(settings: AISettings, model_name: str) -> Agent[AgentDeps, str | DeferredToolRequests]:
    return Agent[AgentDeps, str | DeferredToolRequests](
        model=model_name,
        deps_type=AgentDeps,
        output_type=[str, DeferredToolRequests],
        name="approval-demo-agent",
        instructions="You are a demo approval agent.",
        retries=settings.max_retries,
        toolsets=wrap_toolsets_with_audit(
            wrap_toolsets_with_metadata_approval([_build_approval_demo_toolset()])
        ),
        defer_model_check=True,
    )


def test_list_agents() -> None:
    with TestClient(app) as client:
        response = client.get("/api/v1/agents")

    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 200
    assert body["data"][0]["agent_id"] == "chat-agent"


def test_agent_chat_endpoint() -> None:
    with TestClient(app) as client:
        agent = client.app.state.ai_agent_manager.get_agent("chat-agent")
        with agent.override(model=TestModel(custom_output_text="stubbed chat reply")):
            response = client.post(
                "/api/v1/agents/chat",
                json={"message": "你好，帮我确认服务状态"},
                headers={"x-user-id": "tester"},
            )

    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 200
    assert body["data"]["agent_id"] == "chat-agent"
    assert body["data"]["message"] == "stubbed chat reply"
    assert body["data"]["request_id"]
    assert body["data"]["meta"]["run_kind"] == "chat"
    assert body["data"]["meta"]["history_loaded"] is False
    assert body["data"]["meta"]["history_saved"] is False


def test_agent_chat_session_history_roundtrip() -> None:
    def history_model(messages: list[ModelRequest | ModelResponse], info: AgentInfo) -> ModelResponse:
        del info
        request_texts: list[str] = []
        for message in messages:
            if not isinstance(message, ModelRequest):
                continue
            for part in message.parts:
                content = getattr(part, "content", None)
                if isinstance(content, str):
                    request_texts.append(content)

        latest_prompt = request_texts[-1] if request_texts else ""
        if latest_prompt == "第二次提问":
            if "第一次提问" in request_texts[:-1]:
                return ModelResponse(parts=[TextPart(content="已读取到上一轮历史")])
            return ModelResponse(parts=[TextPart(content="未读取到历史")])

        return ModelResponse(parts=[TextPart(content="首次消息已记录")])

    session_id = "sess-history-demo"
    with TestClient(app) as client:
        history_store = client.app.state.ai_history_store
        asyncio.run(history_store.delete_messages(session_id))
        agent = client.app.state.ai_agent_manager.get_agent("chat-agent")
        with agent.override(model=FunctionModel(history_model)):
            first_response = client.post(
                "/api/v1/agents/chat",
                json={"message": "第一次提问", "session_id": session_id},
                headers={"x-user-id": "tester"},
            )
            second_response = client.post(
                "/api/v1/agents/chat",
                json={"message": "第二次提问", "session_id": session_id},
                headers={"x-user-id": "tester"},
            )
        asyncio.run(history_store.delete_messages(session_id))

    assert first_response.status_code == 200
    first_body = first_response.json()
    assert first_body["code"] == 200
    assert first_body["data"]["message"] == "首次消息已记录"

    assert second_response.status_code == 200
    second_body = second_response.json()
    assert second_body["code"] == 200
    assert second_body["data"]["message"] == "已读取到上一轮历史"
    assert second_body["data"]["session_id"] == session_id
    assert second_body["data"]["meta"]["run_kind"] == "chat"
    assert second_body["data"]["meta"]["history_loaded"] is True
    assert second_body["data"]["meta"]["history_saved"] is True


def test_agent_chat_stream_endpoint() -> None:
    with TestClient(app) as client:
        agent = client.app.state.ai_agent_manager.get_agent("chat-agent")
        with agent.override(model=TestModel(custom_output_text="streamed reply")):
            with client.stream(
                "POST",
                "/api/v1/agents/chat/stream",
                json={"message": "请流式回复", "session_id": "sess-stream"},
                headers={"x-user-id": "tester"},
            ) as response:
                raw = "".join(response.iter_text())

    assert response.status_code == 200
    events = _parse_sse_events(raw)
    assert events[0][0] == "start"
    assert any(event == "delta" for event, _ in events)
    done_event = next(payload for event, payload in events if event == "done")
    assert done_event["status"] == "completed"
    assert done_event["message"] == "streamed reply"
    assert done_event["session_id"] == "sess-stream"
    assert done_event["meta"]["run_kind"] == "stream"


def test_chat_stream_emits_tool_call_and_result_events() -> None:
    def tool_model(messages: list[ModelRequest | ModelResponse], info: AgentInfo) -> ModelResponse:
        del info
        for message in messages:
            if isinstance(message, ModelRequest):
                for part in message.parts:
                    if isinstance(part, ToolReturnPart) and part.tool_name == "get_request_context":
                        return ModelResponse(parts=[TextPart(content="tool executed in stream")])

        return ModelResponse(parts=[ToolCallPart(tool_name="get_request_context", args={})])

    async def tool_stream_model(messages: list[ModelRequest | ModelResponse], info: AgentInfo):
        del info
        for message in messages:
            if isinstance(message, ModelRequest):
                for part in message.parts:
                    if isinstance(part, ToolReturnPart) and part.tool_name == "get_request_context":
                        yield "tool executed in stream"
                        return

        yield {0: DeltaToolCall(name="get_request_context", json_args="{}")}

    with TestClient(app) as client:
        agent = client.app.state.ai_agent_manager.get_agent("chat-agent")
        with agent.override(model=FunctionModel(tool_model, stream_function=tool_stream_model)):
            with client.stream(
                "POST",
                "/api/v1/agents/chat/stream",
                json={"message": "请读取当前请求上下文"},
                headers={"x-user-id": "tester"},
            ) as response:
                raw = "".join(response.iter_text())

    assert response.status_code == 200
    events = _parse_sse_events(raw)
    tool_call_payload = next(payload for event, payload in events if event == "tool_call")
    assert tool_call_payload["tool_name"] == "get_request_context"
    assert tool_call_payload["args"] == {}
    assert tool_call_payload["tool_metadata"]["toolset"]["id"] == "builtin-request-toolset"

    tool_result_payload = next(payload for event, payload in events if event == "tool_result")
    assert tool_result_payload["tool_name"] == "get_request_context"
    assert tool_result_payload["status"] == "success"
    assert tool_result_payload["result"]["user_id"] == "tester"

    done_event = next(payload for event, payload in events if event == "done")
    assert done_event["status"] == "completed"
    assert done_event["message"] == "tool executed in stream"


def test_builtin_toolsets_follows_local_conventions() -> None:
    toolsets = get_builtin_toolsets()

    assert [toolset.id for toolset in toolsets] == [
        "builtin-time-toolset",
        "builtin-request-toolset",
        "builtin-runtime-toolset",
    ]

    for toolset in toolsets:
        assert toolset.strict is True
        assert toolset.require_parameter_descriptions is True

        for tool_name, tool in toolset.tools.items():
            assert tool.name == tool_name
            assert tool.description is not None
            assert tool.description.endswith(".")
            assert tool.strict is True
            assert tool.require_parameter_descriptions is True


def test_builtin_toolsets_split_by_capability_domain() -> None:
    time_toolset = get_builtin_time_toolset()
    request_toolset = get_builtin_request_toolset()
    runtime_toolset = get_builtin_runtime_toolset()

    assert set(time_toolset.tools) == {"get_current_utc_time"}
    assert set(request_toolset.tools) == {"get_request_context"}
    assert set(runtime_toolset.tools) == {
        "get_runtime_config_summary",
        "check_runtime_resources",
    }


def test_metadata_approval_policy_matches_metadata_flags() -> None:
    low_risk_toolset: FunctionToolset[AgentDeps] = create_function_toolset(
        id="test-low-risk-toolset",
        metadata={"toolset": {"id": "test-low-risk-toolset", "kind": "business", "owner": "test"}},
    )

    @low_risk_toolset.tool_plain(
        metadata=build_tool_metadata(category="demo", readonly=False, risk="low", approval_required=False),
    )
    def demo_safe_tool() -> str:
        """Return a safe demo value."""
        return "safe"

    high_risk_toolset: FunctionToolset[AgentDeps] = create_function_toolset(
        id="test-high-risk-toolset",
        metadata={"toolset": {"id": "test-high-risk-toolset", "kind": "business", "owner": "test"}},
    )

    @high_risk_toolset.tool_plain(
        metadata=build_tool_metadata(category="demo", readonly=False, risk="high", approval_required=False),
    )
    def demo_risky_tool() -> str:
        """Return a risky demo value."""
        return "risky"

    deps = _build_test_deps("req-approval")
    ctx = RunContext[AgentDeps](
        deps=deps,
        model=TestModel(),
        usage=RunUsage(),
        prompt="审批策略测试",
    )

    try:
        low_risk_tool = next(iter(low_risk_toolset.tools.values()))
        high_risk_tool = next(iter(high_risk_toolset.tools.values()))
        assert tool_requires_approval(ctx, low_risk_tool.tool_def, {}) is False
        assert tool_requires_approval(ctx, high_risk_tool.tool_def, {}) is True
    finally:
        asyncio.run(deps.http_client.aclose())


async def _assert_approval_wrapper_blocks_high_risk_tool() -> None:
    risky_toolset: FunctionToolset[AgentDeps] = create_function_toolset(
        id="test-approval-block-toolset",
        metadata={"toolset": {"id": "test-approval-block-toolset", "kind": "business", "owner": "test"}},
    )

    @risky_toolset.tool_plain(
        metadata=build_tool_metadata(category="ops", readonly=False, risk="high", approval_required=False),
    )
    def delete_demo_resource() -> str:
        """Delete a demo resource."""
        return "deleted"

    wrapped_toolset = wrap_toolset_with_metadata_approval(risky_toolset)
    deps = _build_test_deps("req-approval-block")
    run_context = RunContext[AgentDeps](
        deps=deps,
        model=TestModel(),
        usage=RunUsage(),
        prompt="执行高风险工具",
    )

    tools = await wrapped_toolset.get_tools(run_context)
    tool = tools["delete_demo_resource"]
    try:
        await wrapped_toolset.call_tool("delete_demo_resource", {}, run_context, tool)
    except ApprovalRequired:
        return
    finally:
        await deps.http_client.aclose()

    raise AssertionError("expected ApprovalRequired to be raised")


def test_metadata_approval_wrapper_blocks_high_risk_tool() -> None:
    asyncio.run(_assert_approval_wrapper_blocks_high_risk_tool())


def test_chat_agent_registers_builtin_toolset_tools() -> None:
    with TestClient(app) as client:
        agent = client.app.state.ai_agent_manager.get_agent("chat-agent")
        tool_model = TestModel(custom_output_text="toolset configured")
        with agent.override(model=tool_model):
            response = client.post(
                "/api/v1/agents/chat",
                json={"message": "当前有哪些 builtin tools"},
                headers={"x-user-id": "tester"},
            )

    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 200
    assert body["data"]["message"] == "toolset configured"

    last_params = tool_model.last_model_request_parameters
    assert last_params is not None
    tool_names = {tool.name for tool in last_params.function_tools}
    assert {
        "get_current_utc_time",
        "get_request_context",
        "get_runtime_config_summary",
        "check_runtime_resources",
    }.issubset(tool_names)

    tool_metadata = {tool.name: tool.metadata or {} for tool in last_params.function_tools}
    assert tool_metadata["get_current_utc_time"]["category"] == "time"
    assert tool_metadata["get_current_utc_time"]["toolset"]["id"] == "builtin-time-toolset"
    assert tool_metadata["get_current_utc_time"]["toolset"]["owner"] == "platform"
    assert tool_metadata["get_request_context"]["readonly"] is True
    assert tool_metadata["get_request_context"]["toolset"]["id"] == "builtin-request-toolset"
    assert tool_metadata["get_request_context"]["risk"] == "low"
    assert tool_metadata["get_request_context"]["approval_required"] is False
    assert "debug" in tool_metadata["get_request_context"]["tags"]


def test_runner_records_latest_tool_exposure() -> None:
    with TestClient(app) as client:
        tool_audit = client.app.state.ai_tool_audit
        tool_audit.clear()
        agent = client.app.state.ai_agent_manager.get_agent("chat-agent")
        tool_model = TestModel(custom_output_text="audit ready")
        with agent.override(model=tool_model):
            response = client.post(
                "/api/v1/agents/chat",
                json={"message": "请说明当前 runtime 配置摘要"},
                headers={"x-user-id": "tester"},
            )

    assert response.status_code == 200
    record = tool_audit.latest_record()
    assert record is not None
    assert record.agent_id == "chat-agent"
    assert record.request_id
    assert "get_runtime_config_summary" in record.tool_names
    assert record.tool_metadata["get_runtime_config_summary"]["category"] == "config"
    assert record.tool_metadata["get_runtime_config_summary"]["toolset"]["id"] == "builtin-runtime-toolset"
    assert record.tool_metadata["get_runtime_config_summary"]["toolset"]["kind"] == "builtin"
    assert record.tool_metadata["get_runtime_config_summary"]["toolset"]["owner"] == "platform"
    assert "readonly" in record.tool_metadata["get_runtime_config_summary"]["tags"]


def test_runner_records_tool_execution_events() -> None:
    def audit_model(messages: list[ModelRequest | ModelResponse], info: AgentInfo) -> ModelResponse:
        for message in messages:
            if isinstance(message, ModelRequest):
                for part in message.parts:
                    if isinstance(part, ToolReturnPart) and part.tool_name == "get_request_context":
                        return ModelResponse(parts=[TextPart(content="tool executed")])

        return ModelResponse(parts=[ToolCallPart(tool_name="get_request_context", args={})])

    with TestClient(app) as client:
        tool_audit = client.app.state.ai_tool_audit
        tool_audit.clear()
        agent = client.app.state.ai_agent_manager.get_agent("chat-agent")
        with agent.override(model=FunctionModel(audit_model)):
            response = client.post(
                "/api/v1/agents/chat",
                json={"message": "请读取本次请求上下文"},
                headers={"x-user-id": "tester"},
            )

    assert response.status_code == 200
    execution_record = tool_audit.latest_execution_record()
    assert execution_record is not None
    assert execution_record.agent_id == "chat-agent"
    assert execution_record.request_id
    assert execution_record.tool_name == "get_request_context"
    assert execution_record.status == "success"
    assert execution_record.tool_args == {}
    assert execution_record.tool_metadata["toolset"]["id"] == "builtin-request-toolset"
    assert execution_record.result["user_id"] == "tester"


def test_chat_approval_resume_flow() -> None:
    def approval_model(messages: list[ModelRequest | ModelResponse], info: AgentInfo) -> ModelResponse:
        del info
        for message in messages:
            if isinstance(message, ModelRequest):
                for part in message.parts:
                    if isinstance(part, ToolReturnPart) and part.tool_name == "delete_demo_resource":
                        return ModelResponse(parts=[TextPart(content="审批后已执行: deleted")])

        return ModelResponse(parts=[ToolCallPart(tool_name="delete_demo_resource", args={})])

    with TestClient(app) as client:
        registry = client.app.state.ai_agent_registry
        manager = client.app.state.ai_agent_manager
        registry.register(
            AgentManifest(
                agent_id="approval-demo-agent",
                name="Approval Demo Agent",
                description="Agent used to test approval resume flow.",
                default_model="test",
            ),
            _build_approval_demo_agent,
        )

        agent = manager.get_agent("approval-demo-agent")
        with agent.override(model=FunctionModel(approval_model)):
            first_response = client.post(
                "/api/v1/agents/chat",
                json={"agent_id": "approval-demo-agent", "message": "请删除演示资源"},
                headers={"x-user-id": "tester"},
            )

            assert first_response.status_code == 200
            first_body = first_response.json()
            assert first_body["code"] == 200
            assert first_body["data"]["status"] == "approval_required"
            assert first_body["data"]["message"] is None

            deferred = first_body["data"]["deferred_tool_requests"]
            assert deferred is not None
            assert deferred["calls"] == []
            assert len(deferred["approvals"]) == 1
            assert deferred["approvals"][0]["tool_name"] == "delete_demo_resource"
            tool_call_id = deferred["approvals"][0]["tool_call_id"]
            message_history_json = deferred["message_history_json"]

            resume_response = client.post(
                "/api/v1/agents/chat/resume",
                json={
                    "agent_id": "approval-demo-agent",
                    "message_history_json": message_history_json,
                    "approvals": [{"tool_call_id": tool_call_id, "approved": True}],
                },
                headers={"x-user-id": "tester"},
            )

    assert resume_response.status_code == 200
    resume_body = resume_response.json()
    assert resume_body["code"] == 200
    assert resume_body["data"]["status"] == "completed"
    assert resume_body["data"]["message"] == "审批后已执行: deleted"
    assert resume_body["data"]["deferred_tool_requests"] is None
    assert resume_body["data"]["meta"]["run_kind"] == "resume"


def test_chat_stream_approval_flow() -> None:
    def approval_model(messages: list[ModelRequest | ModelResponse], info: AgentInfo) -> ModelResponse:
        del messages, info
        return ModelResponse(parts=[ToolCallPart(tool_name="delete_demo_resource", args={})])

    async def approval_stream_model(messages: list[ModelRequest | ModelResponse], info: AgentInfo):
        del messages, info
        yield {0: DeltaToolCall(name="delete_demo_resource", json_args="{}")}

    with TestClient(app) as client:
        registry = client.app.state.ai_agent_registry
        manager = client.app.state.ai_agent_manager
        registry.register(
            AgentManifest(
                agent_id="approval-stream-agent",
                name="Approval Stream Agent",
                description="Agent used to test approval stream flow.",
                default_model="test",
            ),
            _build_approval_demo_agent,
        )

        agent = manager.get_agent("approval-stream-agent")
        with agent.override(model=FunctionModel(approval_model, stream_function=approval_stream_model)):
            with client.stream(
                "POST",
                "/api/v1/agents/chat/stream",
                json={"agent_id": "approval-stream-agent", "message": "请删除演示资源"},
                headers={"x-user-id": "tester"},
            ) as response:
                raw = "".join(response.iter_text())

    assert response.status_code == 200
    events = _parse_sse_events(raw)
    assert events[0][0] == "start"
    approval_pending_event = next(payload for event, payload in events if event == "approval_pending")
    assert approval_pending_event["status"] == "approval_required"
    assert approval_pending_event["deferred_tool_requests"]["approvals"][0]["tool_name"] == "delete_demo_resource"
    approval_event = next(payload for event, payload in events if event == "approval_required")
    assert approval_event["status"] == "approval_required"
    assert approval_event["message"] is None
    assert approval_event["deferred_tool_requests"]["approvals"][0]["tool_name"] == "delete_demo_resource"
    assert approval_event["meta"]["run_kind"] == "stream"


def test_chat_stream_error_payload_contains_run_meta() -> None:
    def broken_model(messages: list[ModelRequest | ModelResponse], info: AgentInfo) -> ModelResponse:
        del messages, info
        raise RuntimeError("stream model exploded")

    with TestClient(app) as client:
        agent = client.app.state.ai_agent_manager.get_agent("chat-agent")
        with agent.override(model=FunctionModel(broken_model)):
            with client.stream(
                "POST",
                "/api/v1/agents/chat/stream",
                json={"message": "请触发错误", "session_id": "sess-stream-error"},
                headers={"x-user-id": "tester"},
            ) as response:
                raw = "".join(response.iter_text())

    assert response.status_code == 200
    events = _parse_sse_events(raw)
    assert events[0][0] == "error"
    error_event = events[0][1]
    assert error_event["error_type"] == "RuntimeError"
    assert error_event["meta"]["run_kind"] == "stream"
    assert error_event["meta"]["stream_mode"] == "fallback"
