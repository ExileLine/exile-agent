from typing import Any

from pydantic_ai import Agent

from app.ai.config import AISettings
from app.ai.deps import AgentDeps
from app.ai.runtime.registry import AgentRegistry
from app.ai.schemas.agent import AgentManifest


class AgentManager:
    """Agent 获取与缓存层。

    它位于 registry 和 runner 之间，负责：
    - 列出 Agent
    - 解析本次请求实际要用的模型
    - 按 `(agent_id, model_name)` 复用 Agent 实例
    """
    def __init__(self, *, registry: AgentRegistry, settings: AISettings) -> None:
        self.registry = registry
        self.settings = settings
        self._cache: dict[tuple[str, str], Agent[AgentDeps, Any]] = {}

    def list_agents(self) -> list[AgentManifest]:
        return self.registry.list_manifests()

    def get_manifest(self, agent_id: str) -> AgentManifest:
        """读取某个 Agent 的 manifest。"""
        return self.registry.get(agent_id).manifest

    def resolve_model(self, agent_id: str, model_name: str | None = None) -> str:
        """解析本次请求最终要使用的模型名。

        优先级是：
        1. 请求显式传入的 `model_name`
        2. Agent manifest 里的 `default_model`
        3. 全局 `AISettings.default_model`
        """
        manifest = self.get_manifest(agent_id)
        print(f"manifest: {manifest}")
        return model_name or manifest.default_model or self.settings.default_model

    def get_agent(
            self,
            agent_id: str,
            model_name: str | None = None,
            *,
            runtime_agent_id: str | None = None,
            model: Any | None = None,
            model_cache_key: str | None = None,
    ) -> Agent[AgentDeps, Any]:
        """获取一个可复用的 Agent 实例。

        当前缓存 key 是 `(agent_id, resolved_model)`，
        这样同一个 Agent 在相同模型下会复用定义实例，避免重复构造。

        `runtime_agent_id` 用于数据库控制面的动态 Agent 配置：
        - `agent_id` 仍作为业务配置、审计和缓存维度
        - `runtime_agent_id` 只决定复用哪个代码里注册的 Agent builder
        """
        builder_agent_id = runtime_agent_id or agent_id
        resolved_model = model_name or self.resolve_model(builder_agent_id, None)
        cache_model_key = model_cache_key or resolved_model
        cache_key = (agent_id, cache_model_key)
        if cache_key not in self._cache:
            registered = self.registry.get(builder_agent_id)
            self._cache[cache_key] = registered.builder(self.settings, model or resolved_model)
        return self._cache[cache_key]
