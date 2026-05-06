from collections.abc import Callable
from typing import Any

from pydantic_ai import Agent

from app.ai.agents.builtin_agents import (
    build_executor_agent,
    build_explore_agent,
    build_general_agent,
    build_planner_agent,
    build_review_agent,
    build_summary_agent,
)
from app.ai.agents.chat_agent import build_chat_agent
from app.ai.config import AISettings
from app.ai.deps import AgentDeps
from app.ai.runtime.registry import AgentRegistry
from app.ai.schemas.agent import AgentManifest


def register_default_agents(registry: AgentRegistry, settings: AISettings) -> None:
    """注册当前项目默认启用的 Agent。

    `chat-agent` 保留为兼容默认入口；通用工作模式 Agent 也统一从这里注册，
    避免在 runtime 初始化时散落注册逻辑。
    """
    registry.register(
        manifest=AgentManifest(
            agent_id="chat-agent",
            name="Chat Agent",
            description="General-purpose assistant agent for the FastAPI service.",
            default_model=settings.default_model,
            supports_stream=False,
        ),
        builder=build_chat_agent,
    )
    for agent_id, name, description, builder in _builtin_mode_agents():
        registry.register(
            manifest=AgentManifest(
                agent_id=agent_id,
                name=name,
                description=description,
                default_model=settings.default_model,
                supports_stream=False,
            ),
            builder=builder,
        )


def _builtin_mode_agents() -> list[tuple[str, str, str, Callable[[AISettings, object], Agent[AgentDeps, Any]]]]:
    return [
        (
            "general-agent",
            "General Agent",
            "通用任务代理，适合普通问答、轻量工具调用和兜底对话。",
            build_general_agent,
        ),
        (
            "explore-agent",
            "Explore Agent",
            "探索代理，适合上下文收集、现状梳理、只读分析和不确定问题调查。",
            build_explore_agent,
        ),
        (
            "planner-agent",
            "Planner Agent",
            "规划代理，适合任务拆解、依赖分析、风险识别和验收标准设计。",
            build_planner_agent,
        ),
        (
            "executor-agent",
            "Executor Agent",
            "执行代理，适合按明确目标或计划完成具体操作、工具调用和流程推进。",
            build_executor_agent,
        ),
        (
            "review-agent",
            "Review Agent",
            "审查代理，适合代码、配置、方案和运行结果的风险审查。",
            build_review_agent,
        ),
        (
            "summary-agent",
            "Summary Agent",
            "总结代理，适合长上下文压缩、纪要整理、结果归纳和后续摘要能力。",
            build_summary_agent,
        ),
    ]
