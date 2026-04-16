from fastapi.testclient import TestClient
from pydantic_ai import models
from pydantic_ai.models.test import TestModel

from app.main import app

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
