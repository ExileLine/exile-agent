import httpx
from fastapi import FastAPI

from app.ai.agents import register_default_agents
from app.ai.config import AISettings
from app.ai.runtime.history import SessionHistoryStore
from app.ai.runtime.manager import AgentManager
from app.ai.runtime.registry import AgentRegistry
from app.ai.runtime.runner import AgentRunner
from app.ai.services.tool_audit import ToolAuditService
from app.core.config import BaseConfig
from app.db import redis_client


async def init_ai_runtime(app: FastAPI, project_config: BaseConfig) -> None:
    """在应用启动阶段初始化 AI runtime 并挂到 `app.state`。

    当前这一步会统一装配：
    - AISettings
    - AgentRegistry
    - AgentManager
    - 共享 http client
    - ToolAuditService
    - SessionHistoryStore
    - AgentRunner

    然后再把它们挂到 `app.state`，供 endpoint 按需取用。
    """
    settings = AISettings.from_config(project_config)
    registry = AgentRegistry()
    register_default_agents(registry, settings)
    manager = AgentManager(registry=registry, settings=settings)
    http_client = httpx.AsyncClient(timeout=settings.http_timeout_seconds)
    tool_audit = ToolAuditService()
    # 会话历史优先落 Redis；
    # 如果当前环境没有 Redis 连接，则退化到进程内存存储，方便测试和本地最小调试。
    history_store = SessionHistoryStore(
        redis=redis_client.redis_pool,
        ttl_seconds=settings.history_ttl_seconds,
    )
    runner = AgentRunner(
        settings=settings,
        agent_manager=manager,
        http_client=http_client,
        tool_audit=tool_audit,
        history_store=history_store,
    )

    app.state.ai_settings = settings
    app.state.ai_agent_registry = registry
    app.state.ai_agent_manager = manager
    app.state.ai_http_client = http_client
    app.state.ai_tool_audit = tool_audit
    app.state.ai_history_store = history_store
    app.state.ai_runner = runner


async def shutdown_ai_runtime(app: FastAPI) -> None:
    """在应用关闭阶段释放 AI runtime 资源并清理 `app.state`。"""
    http_client = getattr(app.state, "ai_http_client", None)
    if http_client is not None:
        await http_client.aclose()

    for attr in (
        "ai_settings",
        "ai_agent_registry",
        "ai_agent_manager",
        "ai_http_client",
        "ai_tool_audit",
        "ai_history_store",
        "ai_runner",
    ):
        if hasattr(app.state, attr):
            delattr(app.state, attr)
