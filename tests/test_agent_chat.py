from fastapi.testclient import TestClient
from pydantic_ai import models
from pydantic_ai.models.test import TestModel

from app.main import app
from app.ai.toolsets.builtin import (
    get_builtin_request_toolset,
    get_builtin_runtime_toolset,
    get_builtin_time_toolset,
    get_builtin_toolsets,
)

models.ALLOW_MODEL_REQUESTS = False


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
