import httpx
from fastapi import FastAPI

from app.ai.agents import register_default_agents
from app.ai.config import AISettings
from app.ai.runtime.manager import AgentManager
from app.ai.runtime.registry import AgentRegistry
from app.ai.runtime.runner import AgentRunner
from app.core.config import BaseConfig


async def init_ai_runtime(app: FastAPI, project_config: BaseConfig) -> None:
    settings = AISettings.from_config(project_config)
    registry = AgentRegistry()
    register_default_agents(registry, settings)
    manager = AgentManager(registry=registry, settings=settings)
    http_client = httpx.AsyncClient(timeout=settings.http_timeout_seconds)
    runner = AgentRunner(
        settings=settings,
        agent_manager=manager,
        http_client=http_client,
    )

    app.state.ai_settings = settings
    app.state.ai_agent_registry = registry
    app.state.ai_agent_manager = manager
    app.state.ai_http_client = http_client
    app.state.ai_runner = runner


async def shutdown_ai_runtime(app: FastAPI) -> None:
    http_client = getattr(app.state, "ai_http_client", None)
    if http_client is not None:
        await http_client.aclose()

    for attr in (
        "ai_settings",
        "ai_agent_registry",
        "ai_agent_manager",
        "ai_http_client",
        "ai_runner",
    ):
        if hasattr(app.state, attr):
            delattr(app.state, attr)
