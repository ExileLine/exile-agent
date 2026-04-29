from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic_ai import Agent

from app.ai.config import AISettings
from app.ai.deps import AgentDeps
from app.ai.exceptions import AgentNotFoundError
from app.ai.schemas.agent import AgentManifest

AgentBuilder = Callable[[AISettings, Any], Agent[AgentDeps, Any]]


@dataclass(slots=True)
class RegisteredAgent:
    """注册表内部保存的 Agent 定义项。"""
    manifest: AgentManifest
    builder: AgentBuilder


class AgentRegistry:
    """Agent 注册表。

    这里只负责回答“系统里有哪些 Agent、它们如何构造”，
    不负责缓存实例，也不负责真正执行模型调用。
    """
    def __init__(self) -> None:
        self._registry: dict[str, RegisteredAgent] = {}

    def register(self, manifest: AgentManifest, builder: AgentBuilder) -> None:
        """注册一个 Agent manifest 和对应 builder。"""
        self._registry[manifest.agent_id] = RegisteredAgent(manifest=manifest, builder=builder)

    def get(self, agent_id: str) -> RegisteredAgent:
        """按 agent_id 获取已注册的 Agent 定义。"""
        try:
            return self._registry[agent_id]
        except KeyError as exc:
            raise AgentNotFoundError(f"未找到 Agent: {agent_id}") from exc

    def list_manifests(self) -> list[AgentManifest]:
        """列出当前已注册的全部 Agent manifest。"""
        return [item.manifest for item in self._registry.values()]
