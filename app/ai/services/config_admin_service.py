from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.config_store.encryption import encrypt_secret, encrypt_secret_mapping
from app.ai.config_store.repository import AIConfigRepository
from app.ai.config_store.schemas import (
    AgentConfigCreate,
    AgentConfigRead,
    AgentConfigUpdate,
    AgentMCPBindingItem,
    AgentMCPBindingRead,
    AIConfigListQuery,
    AIModelCreate,
    AIModelRead,
    AIModelUpdate,
    MCPServerCreate,
    MCPServerRead,
    MCPServerUpdate,
    ModelProviderCreate,
    ModelProviderRead,
    ModelProviderUpdate,
)
from app.ai.config_store.models import (
    AIMCPServer,
    AIModelProvider,
)
from app.ai.exceptions import AIConfigConflictError, AIConfigNotFoundError, AIConfigValidationError
from app.schemas.pagination import query_result


class AIConfigAdminService:
    """AI 控制面配置管理服务。

    endpoint 只负责 HTTP 协议；这里集中处理唯一性校验、secret 加密和返回脱敏。
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repository = AIConfigRepository(session)

    async def list_model_providers(self, query: AIConfigListQuery) -> dict:
        providers, total = await self.repository.paginate_model_providers(
            page=query.page,
            size=query.size,
            keyword=query.keyword,
        )
        records = [_provider_to_read(item).model_dump(mode="json") for item in providers]
        return query_result(records=records, now_page=query.page, total=total)

    async def get_model_provider(self, provider_key: str) -> ModelProviderRead:
        provider = await self.repository.get_model_provider(provider_key)
        if provider is None:
            raise AIConfigNotFoundError(f"未找到模型供应商: {provider_key}")
        return _provider_to_read(provider)

    async def create_model_provider(self, payload: ModelProviderCreate) -> ModelProviderRead:
        if await self.repository.get_model_provider(payload.provider_key) is not None:
            raise AIConfigConflictError(f"模型供应商已存在: {payload.provider_key}")
        provider = await self.repository.create_model_provider(_provider_create_values(payload))
        await self.session.commit()
        return _provider_to_read(provider)

    async def update_model_provider(self, provider_id: int, payload: ModelProviderUpdate) -> ModelProviderRead:
        provider = await self.repository.get_model_provider_by_id(provider_id)
        if provider is None:
            raise AIConfigNotFoundError(f"未找到模型供应商: {provider_id}")
        provider = await self.repository.update_model_provider(provider, _provider_update_values(payload))
        await self.session.commit()
        return _provider_to_read(provider)

    async def list_models(self, query: AIConfigListQuery) -> dict:
        models, total = await self.repository.paginate_models(
            page=query.page,
            size=query.size,
            keyword=query.keyword,
        )
        records = [AIModelRead.model_validate(item).model_dump(mode="json") for item in models]
        return query_result(records=records, now_page=query.page, total=total)

    async def get_model(self, model_key: str) -> AIModelRead:
        model = await self.repository.get_model(model_key)
        if model is None:
            raise AIConfigNotFoundError(f"未找到模型: {model_key}")
        return AIModelRead.model_validate(model)

    async def create_model(self, payload: AIModelCreate) -> AIModelRead:
        if await self.repository.get_model(payload.model_key) is not None:
            raise AIConfigConflictError(f"模型已存在: {payload.model_key}")
        if await self.repository.get_model_provider(payload.provider_key) is None:
            raise AIConfigValidationError(f"模型供应商不存在: {payload.provider_key}")
        model = await self.repository.create_model(payload.model_dump())
        await self.session.commit()
        return AIModelRead.model_validate(model)

    async def update_model(self, model_id: int, payload: AIModelUpdate) -> AIModelRead:
        model = await self.repository.get_model_by_id(model_id)
        if model is None:
            raise AIConfigNotFoundError(f"未找到模型: {model_id}")
        values = payload.model_dump(exclude_unset=True)
        provider_key = values.get("provider_key")
        if provider_key and await self.repository.get_model_provider(provider_key) is None:
            raise AIConfigValidationError(f"模型供应商不存在: {provider_key}")
        model = await self.repository.update_model(model, values)
        await self.session.commit()
        return AIModelRead.model_validate(model)

    async def list_agent_configs(self, query: AIConfigListQuery) -> dict:
        configs, total = await self.repository.paginate_agent_configs(
            page=query.page,
            size=query.size,
            keyword=query.keyword,
        )
        records = [AgentConfigRead.model_validate(item).model_dump(mode="json") for item in configs]
        return query_result(records=records, now_page=query.page, total=total)

    async def get_agent_config(self, agent_id: str) -> AgentConfigRead:
        config = await self.repository.get_agent_config(agent_id)
        if config is None:
            raise AIConfigNotFoundError(f"未找到 Agent 配置: {agent_id}")
        return AgentConfigRead.model_validate(config)

    async def create_agent_config(self, payload: AgentConfigCreate) -> AgentConfigRead:
        if await self.repository.get_agent_config(payload.agent_id) is not None:
            raise AIConfigConflictError(f"Agent 配置已存在: {payload.agent_id}")
        await self._validate_model_keys([payload.default_model_key, *payload.allowed_model_keys_json])
        config = await self.repository.create_agent_config(payload.model_dump())
        await self.session.commit()
        return AgentConfigRead.model_validate(config)

    async def update_agent_config(self, config_id: int, payload: AgentConfigUpdate) -> AgentConfigRead:
        config = await self.repository.get_agent_config_by_id(config_id)
        if config is None:
            raise AIConfigNotFoundError(f"未找到 Agent 配置: {config_id}")
        values = payload.model_dump(exclude_unset=True)
        model_keys = []
        if "default_model_key" in values:
            model_keys.append(values["default_model_key"])
        model_keys.extend(values.get("allowed_model_keys_json") or [])
        await self._validate_model_keys(model_keys)
        config = await self.repository.update_agent_config(config, values)
        await self.session.commit()
        return AgentConfigRead.model_validate(config)

    async def list_mcp_servers(self, query: AIConfigListQuery) -> dict:
        servers, total = await self.repository.paginate_mcp_servers(
            page=query.page,
            size=query.size,
            keyword=query.keyword,
        )
        records = [_mcp_server_to_read(item).model_dump(mode="json") for item in servers]
        return query_result(records=records, now_page=query.page, total=total)

    async def get_mcp_server(self, server_key: str) -> MCPServerRead:
        server = await self.repository.get_mcp_server(server_key)
        if server is None:
            raise AIConfigNotFoundError(f"未找到 MCP server: {server_key}")
        return _mcp_server_to_read(server)

    async def create_mcp_server(self, payload: MCPServerCreate) -> MCPServerRead:
        if await self.repository.get_mcp_server(payload.server_key) is not None:
            raise AIConfigConflictError(f"MCP server 已存在: {payload.server_key}")
        server = await self.repository.create_mcp_server(_mcp_create_values(payload))
        await self.session.commit()
        return _mcp_server_to_read(server)

    async def update_mcp_server(self, server_id: int, payload: MCPServerUpdate) -> MCPServerRead:
        server = await self.repository.get_mcp_server_by_id(server_id)
        if server is None:
            raise AIConfigNotFoundError(f"未找到 MCP server: {server_id}")
        server = await self.repository.update_mcp_server(server, _mcp_update_values(payload))
        await self.session.commit()
        return _mcp_server_to_read(server)

    async def list_agent_mcp_bindings(self, agent_id: str, query: AIConfigListQuery) -> dict:
        bindings, total = await self.repository.paginate_agent_mcp_bindings(
            agent_id=agent_id,
            page=query.page,
            size=query.size,
            keyword=query.keyword,
        )
        records = [AgentMCPBindingRead.model_validate(item).model_dump(mode="json") for item in bindings]
        return query_result(records=records, now_page=query.page, total=total)

    async def replace_agent_mcp_bindings(
        self,
        agent_id: str,
        bindings: list[AgentMCPBindingItem],
    ) -> list[AgentMCPBindingRead]:
        for item in bindings:
            if await self.repository.get_mcp_server(item.server_key) is None:
                raise AIConfigValidationError(f"MCP server 不存在: {item.server_key}")
        new_bindings = await self.repository.replace_agent_mcp_bindings(
            agent_id,
            [item.model_dump() for item in bindings],
        )
        await self.session.commit()
        return [AgentMCPBindingRead.model_validate(item) for item in new_bindings]

    async def _validate_model_keys(self, model_keys: list[str | None]) -> None:
        for model_key in {item for item in model_keys if item}:
            if await self.repository.get_model(model_key) is None:
                raise AIConfigValidationError(f"模型不存在: {model_key}")


def _provider_create_values(payload: ModelProviderCreate) -> dict:
    values = payload.model_dump(exclude={"api_key"})
    values["api_key_encrypted"] = encrypt_secret(payload.api_key)
    return values


def _provider_update_values(payload: ModelProviderUpdate) -> dict:
    values = payload.model_dump(exclude_unset=True, exclude={"api_key"})
    if "api_key" in payload.model_fields_set:
        values["api_key_encrypted"] = encrypt_secret(payload.api_key)
    return values


def _provider_to_read(provider: AIModelProvider) -> ModelProviderRead:
    return ModelProviderRead(
        id=provider.id,
        provider_key=provider.provider_key,
        name=provider.name,
        provider_type=provider.provider_type,
        base_url=provider.base_url,
        enabled=provider.enabled,
        timeout_seconds=provider.timeout_seconds,
        max_retries=provider.max_retries,
        metadata_json=provider.metadata_json or {},
        has_api_key=bool(provider.api_key_encrypted),
    )


def _mcp_create_values(payload: MCPServerCreate) -> dict:
    values = payload.model_dump(exclude={"headers", "env"})
    values["headers_encrypted_json"] = encrypt_secret_mapping(payload.headers)
    values["env_encrypted_json"] = encrypt_secret_mapping(payload.env)
    return values


def _mcp_update_values(payload: MCPServerUpdate) -> dict:
    values = payload.model_dump(exclude_unset=True, exclude={"headers", "env"})
    if "headers" in payload.model_fields_set:
        values["headers_encrypted_json"] = encrypt_secret_mapping(payload.headers)
    if "env" in payload.model_fields_set:
        values["env_encrypted_json"] = encrypt_secret_mapping(payload.env)
    return values


def _mcp_server_to_read(server: AIMCPServer) -> MCPServerRead:
    return MCPServerRead(
        id=server.id,
        server_key=server.server_key,
        name=server.name,
        transport=server.transport,
        command=server.command,
        args_json=server.args_json or [],
        url=server.url,
        header_keys=sorted((server.headers_encrypted_json or {}).keys()),
        env_keys=sorted((server.env_encrypted_json or {}).keys()),
        cwd=server.cwd,
        tool_prefix=server.tool_prefix,
        enabled=server.enabled,
        auto_route_enabled=server.auto_route_enabled,
        route_keywords_json=server.route_keywords_json or [],
        timeout_seconds=server.timeout_seconds,
        read_timeout_seconds=server.read_timeout_seconds,
        max_retries=server.max_retries,
        include_instructions=server.include_instructions,
        risk_level=server.risk_level,
        metadata_json=server.metadata_json or {},
    )
