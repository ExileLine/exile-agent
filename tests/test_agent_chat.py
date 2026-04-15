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
