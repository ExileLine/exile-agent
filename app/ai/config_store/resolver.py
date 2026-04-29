from collections.abc import Sequence

from app.ai.config import AISettings
from app.ai.config_store.encryption import decrypt_secret_mapping
from app.ai.config_store.models import AIAgentConfig, AIAgentMCPBinding, AIMCPServer, AIModel, AIModelProvider
from app.ai.config_store.repository import AIConfigRepository
from app.ai.exceptions import AIConfigValidationError
from app.ai.runtime.resolved_config import (
    ResolvedMCPServerConfig,
    ResolvedModelConfig,
    ResolvedProviderConfig,
    ResolvedRunConfig,
)


class AICapabilityResolver:
    """把请求意图解析成一次 run 最终可用的模型、MCP 和 skills。

    这里是控制面配置进入 runtime 的唯一决策层：
    - 无 DB 配置时 fallback 到当前 `AISettings` 行为
    - 有 DB 配置时执行 enabled、模型 allowlist、MCP binding 校验
    - Runner 后续只消费 `ResolvedRunConfig`，不直接读取多张配置表
    """

    def __init__(self, *, settings: AISettings, repository: AIConfigRepository) -> None:
        self.settings = settings
        self.repository = repository

    async def resolve(
        self,
        *,
        agent_id: str | None,
        requested_model: str | None = None,
        requested_mcp_servers: Sequence[str] | None = None,
        requested_skill_ids: Sequence[str] | None = None,
        route_message: str | None = None,
    ) -> ResolvedRunConfig:
        resolved_agent_id = agent_id or self.settings.default_agent
        agent_config = await self.repository.get_agent_config(resolved_agent_id)
        if agent_config is None:
            return self._fallback_config(
                agent_id=resolved_agent_id,
                requested_model=requested_model,
                requested_mcp_servers=requested_mcp_servers,
                requested_skill_ids=requested_skill_ids,
            )

        if not agent_config.enabled:
            raise AIConfigValidationError(f"Agent 配置已停用: {resolved_agent_id}")

        model, provider = await self._resolve_model(agent_config=agent_config, requested_model=requested_model)
        mcp_servers = await self._resolve_mcp_servers(
            agent_config=agent_config,
            requested_mcp_servers=requested_mcp_servers,
            route_message=route_message,
        )
        skill_ids = _dedupe([*agent_config.default_skill_ids_json, *(requested_skill_ids or [])])

        return ResolvedRunConfig(
            agent_id=resolved_agent_id,
            model=model,
            provider=provider,
            mcp_servers=tuple(mcp_servers),
            skill_ids=tuple(skill_ids),
            source="database",
            config_version=_build_config_version(agent_config),
            runtime_flags={
                "allow_request_model_override": agent_config.allow_request_model_override,
                "allow_request_mcp_override": agent_config.allow_request_mcp_override,
                "supports_stream": agent_config.supports_stream,
            },
        )

    def _fallback_config(
        self,
        *,
        agent_id: str,
        requested_model: str | None,
        requested_mcp_servers: Sequence[str] | None,
        requested_skill_ids: Sequence[str] | None,
    ) -> ResolvedRunConfig:
        model_name = requested_model or self.settings.default_model
        return ResolvedRunConfig(
            agent_id=agent_id,
            model=ResolvedModelConfig(
                model_key=model_name,
                provider_key=None,
                model_name=model_name,
            ),
            mcp_servers=tuple(
                ResolvedMCPServerConfig(
                    server_key=server_key,
                    transport="unknown",
                    tool_prefix=None,
                )
                for server_key in _dedupe(requested_mcp_servers or [])
            ),
            skill_ids=tuple(_dedupe(requested_skill_ids or [])),
            source="settings_fallback",
            runtime_flags={
                "allow_request_model_override": True,
                "allow_request_mcp_override": True,
                "supports_stream": True,
            },
        )

    async def _resolve_model(
        self,
        *,
        agent_config: AIAgentConfig,
        requested_model: str | None,
    ) -> tuple[ResolvedModelConfig, ResolvedProviderConfig | None]:
        selected_model_key = await self._select_model_key(agent_config=agent_config, requested_model=requested_model)
        model = await self.repository.get_enabled_model(selected_model_key)
        if model is None:
            raise AIConfigValidationError(f"模型未启用或不存在: {selected_model_key}")

        provider = await self.repository.get_enabled_model_provider(model.provider_key)
        if provider is None:
            raise AIConfigValidationError(f"模型供应商未启用或不存在: {model.provider_key}")

        return _model_to_resolved(model), _provider_to_resolved(provider)

    async def _select_model_key(self, *, agent_config: AIAgentConfig, requested_model: str | None) -> str:
        allowed_model_keys = set(agent_config.allowed_model_keys_json or [])

        if requested_model:
            if not agent_config.allow_request_model_override:
                raise AIConfigValidationError(f"Agent 不允许请求覆盖模型: {agent_config.agent_id}")
            if allowed_model_keys and requested_model not in allowed_model_keys:
                raise AIConfigValidationError(f"模型不在 Agent allowlist 中: {requested_model}")
            return requested_model

        default_model_key = agent_config.default_model_key or self.settings.default_model
        if allowed_model_keys and default_model_key not in allowed_model_keys:
            raise AIConfigValidationError(f"默认模型不在 Agent allowlist 中: {default_model_key}")
        return default_model_key

    async def _resolve_mcp_servers(
        self,
        *,
        agent_config: AIAgentConfig,
        requested_mcp_servers: Sequence[str] | None,
        route_message: str | None,
    ) -> list[ResolvedMCPServerConfig]:
        bindings = await self.repository.list_agent_mcp_bindings(agent_config.agent_id)
        binding_by_server_key = {binding.server_key: binding for binding in bindings}

        if requested_mcp_servers:
            if not agent_config.allow_request_mcp_override:
                raise AIConfigValidationError(f"Agent 不允许请求覆盖 MCP: {agent_config.agent_id}")
            selected_server_keys = _dedupe(requested_mcp_servers)
        elif agent_config.default_mcp_server_ids_json:
            selected_server_keys = _dedupe(agent_config.default_mcp_server_ids_json or [])
        else:
            return await self._auto_route_mcp_servers(bindings=bindings, route_message=route_message)

        resolved: list[ResolvedMCPServerConfig] = []
        for server_key in selected_server_keys:
            binding = binding_by_server_key.get(server_key)
            if binding is None:
                raise AIConfigValidationError(f"MCP server 未绑定到 Agent: {server_key}")
            server = await self.repository.get_mcp_server(server_key)
            if server is None or not server.enabled:
                raise AIConfigValidationError(f"MCP server 未启用或不存在: {server_key}")
            resolved.append(_mcp_to_resolved(server, binding))
        return resolved

    async def _auto_route_mcp_servers(
        self,
        *,
        bindings: Sequence[AIAgentMCPBinding],
        route_message: str | None,
    ) -> list[ResolvedMCPServerConfig]:
        """从已绑定 MCP 中按消息关键词安全自动路由。

        自动路由只会在数据库绑定 allowlist 内发生，不会读取 settings 中的全局 MCP 池。
        """

        normalized_message = _normalize_text(route_message or "")
        if not normalized_message:
            return []

        resolved: list[ResolvedMCPServerConfig] = []
        for binding in bindings:
            if not binding.allow_auto_route:
                continue
            server = await self.repository.get_mcp_server(binding.server_key)
            if server is None or server.enabled is False or server.auto_route_enabled is False:
                continue
            if _mcp_server_matches_message(server, normalized_message):
                resolved.append(_mcp_to_resolved(server, binding))
        return resolved


def _model_to_resolved(model: AIModel) -> ResolvedModelConfig:
    return ResolvedModelConfig(
        model_key=model.model_key,
        provider_key=model.provider_key,
        model_name=model.model_name,
        supports_stream=model.supports_stream,
        supports_tools=model.supports_tools,
        supports_json_output=model.supports_json_output,
        risk_level=model.risk_level,
    )


def _provider_to_resolved(provider: AIModelProvider) -> ResolvedProviderConfig:
    return ResolvedProviderConfig(
        provider_key=provider.provider_key,
        provider_type=provider.provider_type,
        base_url=provider.base_url,
        api_key_encrypted=provider.api_key_encrypted,
        timeout_seconds=provider.timeout_seconds,
        max_retries=provider.max_retries,
    )


def _mcp_to_resolved(server: AIMCPServer, binding: AIAgentMCPBinding) -> ResolvedMCPServerConfig:
    return ResolvedMCPServerConfig(
        server_key=server.server_key,
        transport=server.transport,
        tool_prefix=server.tool_prefix,
        command=server.command,
        args=tuple(str(item) for item in (server.args_json or [])),
        url=server.url,
        headers=decrypt_secret_mapping(server.headers_encrypted_json),
        env=decrypt_secret_mapping(server.env_encrypted_json),
        cwd=server.cwd,
        auto_route_enabled=server.auto_route_enabled,
        route_keywords=tuple(str(item) for item in (server.route_keywords_json or [])),
        timeout_seconds=server.timeout_seconds,
        read_timeout_seconds=server.read_timeout_seconds,
        max_retries=server.max_retries,
        include_instructions=server.include_instructions,
        required_approval=binding.required_approval,
        allow_auto_route=binding.allow_auto_route,
        risk_level=server.risk_level,
    )


def _build_config_version(agent_config: AIAgentConfig) -> str:
    return f"agent:{agent_config.agent_id}:{agent_config.update_timestamp}"


def _dedupe(values: Sequence[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = value.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(normalized)
    return deduped


def _mcp_server_matches_message(server: AIMCPServer, normalized_message: str) -> bool:
    for keyword in _mcp_route_keywords(server):
        if keyword in normalized_message:
            return True
    return False


def _mcp_route_keywords(server: AIMCPServer) -> list[str]:
    keywords: list[str] = []
    seen: set[str] = set()
    for raw_keyword in [
        *(server.route_keywords_json or []),
        server.server_key,
        server.tool_prefix or "",
    ]:
        normalized_keyword = _normalize_text(str(raw_keyword))
        if not normalized_keyword or normalized_keyword in seen:
            continue
        seen.add(normalized_keyword)
        keywords.append(normalized_keyword)
    return keywords


def _normalize_text(value: str) -> str:
    return " ".join(value.casefold().split())
