import asyncio
from typing import Any

from app.ai.config import AISettings
from app.ai.config_store.models import AIAgentConfig, AIAgentMCPBinding, AIMCPServer, AIModel, AIModelProvider
from app.ai.config_store.resolver import AICapabilityResolver
from app.ai.exceptions import AIConfigValidationError


class _ResolverRepository:
    def __init__(self, *, agent_config: AIAgentConfig | None = None) -> None:
        self.agent_config = agent_config
        self.models: dict[str, AIModel] = {}
        self.providers: dict[str, AIModelProvider] = {}
        self.mcp_servers: dict[str, AIMCPServer] = {}
        self.bindings: list[AIAgentMCPBinding] = []

    async def get_agent_config(self, agent_id: str) -> AIAgentConfig | None:
        if self.agent_config is not None and self.agent_config.agent_id == agent_id:
            return self.agent_config
        return None

    async def get_enabled_model(self, model_key: str) -> AIModel | None:
        model = self.models.get(model_key)
        if model is None or not model.enabled:
            return None
        return model

    async def get_enabled_model_provider(self, provider_key: str) -> AIModelProvider | None:
        provider = self.providers.get(provider_key)
        if provider is None or not provider.enabled:
            return None
        return provider

    async def list_agent_mcp_bindings(self, agent_id: str) -> list[AIAgentMCPBinding]:
        return [binding for binding in self.bindings if binding.agent_id == agent_id and binding.enabled]

    async def get_mcp_server(self, server_key: str) -> AIMCPServer | None:
        return self.mcp_servers.get(server_key)


def _agent_config(**overrides: Any) -> AIAgentConfig:
    values = {
        "agent_id": "chat-agent",
        "enabled": True,
        "default_model_key": "deepseek-chat",
        "allowed_model_keys_json": ["deepseek-chat", "deepseek-reasoner"],
        "default_skill_ids_json": ["ops-observer"],
        "default_mcp_server_ids_json": ["docs"],
        "allow_request_model_override": True,
        "allow_request_mcp_override": True,
        "supports_stream": True,
        "update_timestamp": 123,
    }
    values.update(overrides)
    return AIAgentConfig(**values)


def _model(model_key: str = "deepseek-chat", *, enabled: bool = True) -> AIModel:
    return AIModel(
        model_key=model_key,
        provider_key="deepseek",
        model_name=model_key,
        enabled=enabled,
        supports_stream=True,
        supports_tools=True,
        supports_json_output=False,
        risk_level="low",
    )


def _provider(*, enabled: bool = True) -> AIModelProvider:
    return AIModelProvider(
        provider_key="deepseek",
        name="DeepSeek",
        provider_type="openai_compatible",
        enabled=enabled,
    )


def _mcp_server(server_key: str = "docs", *, enabled: bool = True) -> AIMCPServer:
    return AIMCPServer(
        server_key=server_key,
        name="Docs",
        transport="streamable-http",
        url="https://example.com/mcp",
        enabled=enabled,
        risk_level="low",
    )


def _binding(server_key: str = "docs") -> AIAgentMCPBinding:
    return AIAgentMCPBinding(
        agent_id="chat-agent",
        server_key=server_key,
        enabled=True,
        required_approval=True,
        allow_auto_route=False,
    )


def test_resolver_falls_back_to_settings_when_agent_config_missing() -> None:
    async def run() -> None:
        repository = _ResolverRepository()
        resolver = AICapabilityResolver(settings=AISettings(default_model="openai:gpt-5.2"), repository=repository)  # type: ignore[arg-type]

        resolved = await resolver.resolve(
            agent_id="chat-agent",
            requested_model="openai:gpt-5.4",
            requested_mcp_servers=["demo"],
            requested_skill_ids=["ops-observer"],
        )

        assert resolved.source == "settings_fallback"
        assert resolved.agent_id == "chat-agent"
        assert resolved.model_name == "openai:gpt-5.4"
        assert resolved.mcp_server_keys == ["demo"]
        assert resolved.skill_ids == ("ops-observer",)

    asyncio.run(run())


def test_resolver_uses_database_config_and_merges_defaults() -> None:
    async def run() -> None:
        repository = _ResolverRepository(agent_config=_agent_config())
        repository.models["deepseek-reasoner"] = _model("deepseek-reasoner")
        repository.providers["deepseek"] = _provider()
        repository.mcp_servers["docs"] = _mcp_server("docs")
        repository.bindings.append(_binding("docs"))
        resolver = AICapabilityResolver(settings=AISettings(), repository=repository)  # type: ignore[arg-type]

        resolved = await resolver.resolve(
            agent_id="chat-agent",
            requested_model="deepseek-reasoner",
            requested_mcp_servers=["docs"],
            requested_skill_ids=["custom-skill"],
        )

        assert resolved.source == "database"
        assert resolved.model_key == "deepseek-reasoner"
        assert resolved.provider is not None
        assert resolved.provider.provider_key == "deepseek"
        assert resolved.mcp_server_keys == ["docs"]
        assert resolved.mcp_servers[0].required_approval is True
        assert resolved.mcp_servers[0].allow_auto_route is False
        assert resolved.skill_ids == ("ops-observer", "custom-skill")
        assert resolved.runtime_flags["allow_request_model_override"] is True

    asyncio.run(run())


def test_resolver_rejects_model_outside_agent_allowlist() -> None:
    async def run() -> None:
        repository = _ResolverRepository(agent_config=_agent_config())
        resolver = AICapabilityResolver(settings=AISettings(), repository=repository)  # type: ignore[arg-type]

        try:
            await resolver.resolve(agent_id="chat-agent", requested_model="not-allowed")
        except AIConfigValidationError as exc:
            assert "allowlist" in str(exc)
            return
        raise AssertionError("expected AIConfigValidationError")

    asyncio.run(run())


def test_resolver_rejects_unbound_requested_mcp_server() -> None:
    async def run() -> None:
        repository = _ResolverRepository(agent_config=_agent_config())
        repository.models["deepseek-chat"] = _model()
        repository.providers["deepseek"] = _provider()
        repository.mcp_servers["filesystem"] = _mcp_server("filesystem")
        resolver = AICapabilityResolver(settings=AISettings(), repository=repository)  # type: ignore[arg-type]

        try:
            await resolver.resolve(agent_id="chat-agent", requested_mcp_servers=["filesystem"])
        except AIConfigValidationError as exc:
            assert "未绑定" in str(exc)
            return
        raise AssertionError("expected AIConfigValidationError")

    asyncio.run(run())
