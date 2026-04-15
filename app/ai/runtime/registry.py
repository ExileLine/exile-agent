from collections.abc import Callable
from dataclasses import dataclass

from pydantic_ai import Agent

from app.ai.config import AISettings
from app.ai.deps import AgentDeps
from app.ai.exceptions import AgentNotFoundError
from app.ai.schemas.agent import AgentManifest

AgentBuilder = Callable[[AISettings, str], Agent[AgentDeps, str]]


@dataclass(slots=True)
class RegisteredAgent:
    manifest: AgentManifest
    builder: AgentBuilder


class AgentRegistry:
    def __init__(self) -> None:
        self._registry: dict[str, RegisteredAgent] = {}

    def register(self, manifest: AgentManifest, builder: AgentBuilder) -> None:
        self._registry[manifest.agent_id] = RegisteredAgent(manifest=manifest, builder=builder)

    def get(self, agent_id: str) -> RegisteredAgent:
        try:
            return self._registry[agent_id]
        except KeyError as exc:
            raise AgentNotFoundError(f"未找到 Agent: {agent_id}") from exc

    def list_manifests(self) -> list[AgentManifest]:
        return [item.manifest for item in self._registry.values()]
