import asyncio
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from app.ai.mcp import (
    MCPManager,
    ManagedMCPServerConfig,
    ManagedMCPServerSSEConfig,
    ManagedMCPServerStdioConfig,
    ManagedMCPServerStreamableHTTPConfig,
    parse_mcp_servers_json,
)
from app.ai.runtime import init_ai_runtime, shutdown_ai_runtime
from app.ai.runtime.resolved_config import ResolvedMCPServerConfig
from app.ai.skills import SkillResolution
from app.ai.toolsets.conventions import create_function_toolset
from app.ai.toolsets.metadata import build_tool_metadata, build_toolset_metadata
from app.core.config import BaseConfig
from app.main import app


def _build_demo_mcp_toolset():
    toolset = create_function_toolset(
        id="demo-mcp-toolset",
        metadata=build_toolset_metadata(
            toolset_id="demo-mcp-toolset",
            kind="mcp",
            owner="test",
            readonly=True,
        ),
    )

    @toolset.tool_plain(
        metadata=build_tool_metadata(category="mcp", readonly=True, risk="low"),
    )
    def demo_mcp_ping() -> str:
        """Return a fixed demo MCP payload."""

        return "mcp-pong"

    return toolset


def test_parse_mcp_servers_json_supports_map_and_transport_inference() -> None:
    raw_json = json.dumps(
        {
            "mcpServers": {
                "filesystem": {
                    "command": "uvx",
                    "args": ["mcp-server-filesystem", "."],
                },
                "remote-docs": {
                    "url": "http://localhost:3001/sse",
                },
                "maps": {
                    "transport": "streamable-http",
                    "url": "https://example.com/maps/mcp",
                    "route_keywords": ["地图", "路线", "导航"],
                },
            }
        }
    )

    servers = parse_mcp_servers_json(raw_json)

    assert len(servers) == 3
    assert isinstance(servers[0], ManagedMCPServerStdioConfig)
    assert servers[0].id == "filesystem"
    assert servers[0].transport == "stdio"
    assert isinstance(servers[1], ManagedMCPServerSSEConfig)
    assert servers[1].id == "remote-docs"
    assert servers[1].transport == "sse"
    assert isinstance(servers[2], ManagedMCPServerStreamableHTTPConfig)
    assert servers[2].id == "maps"
    assert servers[2].route_keywords == ["地图", "路线", "导航"]


def test_init_ai_runtime_mounts_mcp_manager() -> None:
    test_app = FastAPI()
    config = BaseConfig(
        AI_ENABLE_MCP=True,
        AI_MCP_SERVERS_JSON=json.dumps(
            {
                "mcpServers": {
                    "filesystem": {
                        "transport": "stdio",
                        "command": "uvx",
                        "args": ["mcp-server-filesystem", "."],
                    }
                }
            }
        ),
    )

    asyncio.run(init_ai_runtime(test_app, config))
    try:
        mcp_manager = test_app.state.ai_mcp_manager
        assert isinstance(mcp_manager, MCPManager)
        assert [server.id for server in mcp_manager.list_servers()] == ["filesystem"]
    finally:
        asyncio.run(shutdown_ai_runtime(test_app))


def test_agent_chat_endpoint_can_attach_request_mcp_toolsets(monkeypatch) -> None:
    def mcp_model(messages: list[ModelRequest | ModelResponse], info: AgentInfo) -> ModelResponse:
        del info
        for message in messages:
            if isinstance(message, ModelRequest):
                for part in message.parts:
                    if isinstance(part, ToolReturnPart) and part.tool_name == "demo_mcp_ping":
                        return ModelResponse(parts=[TextPart(content="MCP tool executed")])

        return ModelResponse(parts=[ToolCallPart(tool_name="demo_mcp_ping", args={})])

    with TestClient(app) as client:
        runner = client.app.state.ai_runner
        agent = client.app.state.ai_agent_manager.get_agent("chat-agent")
        monkeypatch.setattr(
            runner.mcp_manager,
            "build_toolsets",
            lambda server_ids: [_build_demo_mcp_toolset()] if server_ids == ["demo-server"] else [],
        )

        with agent.override(model=FunctionModel(mcp_model)):
            response = client.post(
                "/api/v1/agents/chat",
                json={"message": "请调用 MCP 工具", "mcp_servers": ["demo-server"]},
                headers={"x-user-id": "tester"},
            )

    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 200
    assert body["data"]["message"] == "MCP tool executed"
    assert body["data"]["meta"]["mcp_servers"] == ["demo-server"]


def test_mcp_manager_can_auto_route_server_ids() -> None:
    manager = MCPManager(
        enabled=True,
        server_configs=[
            ManagedMCPServerStreamableHTTPConfig(
                id="maps",
                url="https://example.com/maps/mcp",
                route_keywords=["地图", "路线", "导航"],
            ),
            ManagedMCPServerStreamableHTTPConfig(
                id="weather",
                url="https://example.com/weather/mcp",
                route_keywords=["天气", "温度"],
            ),
        ],
        http_client=httpx.AsyncClient(),
    )

    assert manager.resolve_server_ids(requested_server_ids=["weather"], message="请看地图") == ["weather"]
    assert manager.resolve_server_ids(requested_server_ids=None, message="请帮我查上海地图路线") == ["maps"]
    assert manager.resolve_server_ids(requested_server_ids=None, message="查询上海天气和温度") == ["weather"]
    assert manager.resolve_server_ids(requested_server_ids=None, message="地图和天气一起查") == ["maps", "weather"]


def test_agent_chat_endpoint_can_auto_route_mcp_toolsets(monkeypatch) -> None:
    def mcp_model(messages: list[ModelRequest | ModelResponse], info: AgentInfo) -> ModelResponse:
        del info
        for message in messages:
            if isinstance(message, ModelRequest):
                for part in message.parts:
                    if isinstance(part, ToolReturnPart) and part.tool_name == "demo_mcp_ping":
                        return ModelResponse(parts=[TextPart(content="auto-routed MCP tool executed")])

        return ModelResponse(parts=[ToolCallPart(tool_name="demo_mcp_ping", args={})])

    with TestClient(app) as client:
        runner = client.app.state.ai_runner
        agent = client.app.state.ai_agent_manager.get_agent("chat-agent")
        runner.mcp_manager._config_by_id = {
            "demo-server": ManagedMCPServerStdioConfig(
                id="demo-server",
                command="python",
                args=["scripts/mock_mcp_server.py"],
                route_keywords=["演示MCP", "demo mcp"],
            )
        }
        monkeypatch.setattr(
            runner.mcp_manager,
            "build_toolsets",
            lambda server_ids: [_build_demo_mcp_toolset()] if server_ids == ["demo-server"] else [],
        )

        with agent.override(model=FunctionModel(mcp_model)):
            response = client.post(
                "/api/v1/agents/chat",
                json={"message": "请使用演示MCP确认当前能力是否可用"},
                headers={"x-user-id": "tester"},
            )

    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 200
    assert body["data"]["message"] == "auto-routed MCP tool executed"
    assert body["data"]["meta"]["mcp_servers"] == ["demo-server"]


def test_runner_can_disable_legacy_mcp_auto_route_for_config_control_plane() -> None:
    with TestClient(app) as client:
        runner = client.app.state.ai_runner
        runner.mcp_manager._config_by_id = {
            "demo-server": ManagedMCPServerStdioConfig(
                id="demo-server",
                command="python",
                args=["scripts/mock_mcp_server.py"],
                route_keywords=["演示MCP", "demo mcp"],
            )
        }

        resolved_server_ids, toolsets = runner._resolve_request_toolsets(
            mcp_server_ids=[],
            route_message="请使用演示MCP确认当前能力是否可用",
            skill_resolution=SkillResolution(
                skills=(),
                instructions=(),
                required_toolset_ids=(),
                required_mcp_server_ids=(),
            ),
            allow_auto_route=False,
        )

    assert resolved_server_ids == []
    assert toolsets == []


def test_mcp_manager_can_build_toolsets_from_database_configs(monkeypatch) -> None:
    manager = MCPManager(enabled=True, server_configs=[], http_client=httpx.AsyncClient())
    built_configs: list[ManagedMCPServerConfig] = []

    def build_server(config: ManagedMCPServerConfig):
        built_configs.append(config)
        return _build_demo_mcp_toolset()

    monkeypatch.setattr(manager, "_build_server", build_server)

    toolsets = manager.build_toolsets_from_configs(
        [
            ManagedMCPServerStreamableHTTPConfig(
                id="docs",
                url="https://example.com/mcp",
                headers={"authorization": "Bearer token"},
                tool_prefix="docs",
            )
        ]
    )
    second_toolsets = manager.build_toolsets_from_configs(
        [
            ManagedMCPServerStreamableHTTPConfig(
                id="docs",
                url="https://example.com/mcp",
                headers={"authorization": "Bearer token"},
                tool_prefix="docs",
            )
        ]
    )

    assert len(toolsets) == 1
    assert len(second_toolsets) == 1
    assert len(built_configs) == 1
    assert built_configs[0].headers == {"authorization": "Bearer token"}


def test_runner_converts_resolved_database_mcp_config_to_managed_config() -> None:
    with TestClient(app) as client:
        runner = client.app.state.ai_runner
        managed = runner._build_managed_mcp_config(
            ResolvedMCPServerConfig(
                server_key="docs",
                transport="streamable-http",
                tool_prefix="docs",
                url="https://example.com/mcp",
                headers={"authorization": "Bearer token"},
                route_keywords=("文档",),
                timeout_seconds=8.0,
                read_timeout_seconds=60.0,
                max_retries=2,
                include_instructions=True,
            )
        )

    assert isinstance(managed, ManagedMCPServerStreamableHTTPConfig)
    assert managed.id == "docs"
    assert managed.url == "https://example.com/mcp"
    assert managed.headers == {"authorization": "Bearer token"}
    assert managed.route_keywords == ["文档"]
    assert managed.timeout == 8.0
    assert managed.read_timeout == 60.0
    assert managed.max_retries == 2
    assert managed.include_instructions is True
