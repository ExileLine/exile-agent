from pydantic_ai import Agent

from app.ai.config import AISettings
from app.ai.deps import AgentDeps
from app.ai.runtime.registry import AgentRegistry
from app.ai.schemas.agent import AgentManifest


class AgentManager:
    def __init__(self, *, registry: AgentRegistry, settings: AISettings) -> None:
        self.registry = registry
        self.settings = settings
        self._cache: dict[tuple[str, str], Agent[AgentDeps, str]] = {}

    def list_agents(self) -> list[AgentManifest]:
        return self.registry.list_manifests()

    def get_manifest(self, agent_id: str) -> AgentManifest:
        return self.registry.get(agent_id).manifest

    def resolve_model(self, agent_id: str, model_name: str | None = None) -> str:
        manifest = self.get_manifest(agent_id)
        return model_name or manifest.default_model or self.settings.default_model

    def get_agent(self, agent_id: str, model_name: str | None = None) -> Agent[AgentDeps, str]:
        resolved_model = self.resolve_model(agent_id, model_name)
        cache_key = (agent_id, resolved_model)
        if cache_key not in self._cache:
            registered = self.registry.get(agent_id)
            self._cache[cache_key] = registered.builder(self.settings, resolved_model)
        return self._cache[cache_key]
