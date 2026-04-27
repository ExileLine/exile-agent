from collections.abc import Sequence
from typing import Any

from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.config_store.models import (
    AIAgentConfig,
    AIAgentMCPBinding,
    AIMCPServer,
    AIModel,
    AIModelProvider,
)
from app.schemas.pagination import page_size


class AIConfigRepository:
    """AI 控制面配置查询入口。

    这一层只负责读取数据库配置，不承担运行时策略决策。
    后续 `AICapabilityResolver` 会基于这些查询结果做 allowlist、fallback 和灰度解析。
    """

    def __init__(self, session: AsyncSession) -> None:
        # repository 不拥有 session 生命周期，也不 commit。
        # 调用方 service 负责事务边界，这样多个 repository 操作可以组成一个原子业务动作。
        self.session = session

    async def get_enabled_model_provider(self, provider_key: str) -> AIModelProvider | None:
        """读取运行时可用的 provider。

        enabled 查询用于 resolver/runtime；管理接口通常使用非 enabled 版本，
        这样被停用的配置仍然可以被查看和重新启用。
        """
        stmt = _active_select(AIModelProvider).where(
            AIModelProvider.provider_key == provider_key,
            AIModelProvider.enabled.is_(True),
        )
        return await self._scalar_one_or_none(stmt)

    async def get_model_provider(self, provider_key: str) -> AIModelProvider | None:
        """读取未逻辑删除的 provider，不按 enabled 过滤。"""
        stmt = _active_select(AIModelProvider).where(AIModelProvider.provider_key == provider_key)
        return await self._scalar_one_or_none(stmt)

    async def get_model_provider_by_id(self, provider_id: int) -> AIModelProvider | None:
        """按数据库主键读取 provider，供编辑接口使用。"""
        stmt = _active_select(AIModelProvider).where(AIModelProvider.id == provider_id)
        return await self._scalar_one_or_none(stmt)

    async def list_enabled_model_providers(self) -> list[AIModelProvider]:
        stmt = _active_select(AIModelProvider).where(AIModelProvider.enabled.is_(True)).order_by(
            AIModelProvider.provider_key
        )
        return await self._scalars_all(stmt)

    async def list_model_providers(self) -> list[AIModelProvider]:
        stmt = _active_select(AIModelProvider).order_by(AIModelProvider.provider_key)
        return await self._scalars_all(stmt)

    async def paginate_model_providers(
        self,
        *,
        page: int,
        size: int,
        keyword: str | None,
    ) -> tuple[list[AIModelProvider], int]:
        """分页查询 provider，支持按 key/name/type/base_url 模糊搜索。"""
        return await self._paginate(
            model=AIModelProvider,
            page=page,
            size=size,
            keyword=keyword,
            search_columns=[
                AIModelProvider.provider_key,
                AIModelProvider.name,
                AIModelProvider.provider_type,
                AIModelProvider.base_url,
            ],
            order_column=AIModelProvider.provider_key,
        )

    async def create_model_provider(self, values: dict[str, Any]) -> AIModelProvider:
        provider = AIModelProvider(**values)
        return await self._add(provider)

    async def update_model_provider(self, provider: AIModelProvider, values: dict[str, Any]) -> AIModelProvider:
        return await self._update(provider, values)

    async def get_enabled_model(self, model_key: str) -> AIModel | None:
        """读取运行时可用的模型配置。"""
        stmt = _active_select(AIModel).where(
            AIModel.model_key == model_key,
            AIModel.enabled.is_(True),
        )
        return await self._scalar_one_or_none(stmt)

    async def get_model(self, model_key: str) -> AIModel | None:
        """读取未逻辑删除的模型配置，不按 enabled 过滤。"""
        stmt = _active_select(AIModel).where(AIModel.model_key == model_key)
        return await self._scalar_one_or_none(stmt)

    async def get_model_by_id(self, model_id: int) -> AIModel | None:
        """按数据库主键读取模型配置，供编辑接口使用。"""
        stmt = _active_select(AIModel).where(AIModel.id == model_id)
        return await self._scalar_one_or_none(stmt)

    async def list_enabled_models(self) -> list[AIModel]:
        stmt = _active_select(AIModel).where(AIModel.enabled.is_(True)).order_by(AIModel.model_key)
        return await self._scalars_all(stmt)

    async def list_models(self) -> list[AIModel]:
        stmt = _active_select(AIModel).order_by(AIModel.model_key)
        return await self._scalars_all(stmt)

    async def paginate_models(
        self,
        *,
        page: int,
        size: int,
        keyword: str | None,
    ) -> tuple[list[AIModel], int]:
        """分页查询模型，支持按 key/provider/model_name/display_name 模糊搜索。"""
        return await self._paginate(
            model=AIModel,
            page=page,
            size=size,
            keyword=keyword,
            search_columns=[
                AIModel.model_key,
                AIModel.provider_key,
                AIModel.model_name,
                AIModel.display_name,
            ],
            order_column=AIModel.model_key,
        )

    async def create_model(self, values: dict[str, Any]) -> AIModel:
        model = AIModel(**values)
        return await self._add(model)

    async def update_model(self, model: AIModel, values: dict[str, Any]) -> AIModel:
        return await self._update(model, values)

    async def get_agent_config(self, agent_id: str) -> AIAgentConfig | None:
        """读取 Agent 配置。

        这里不按 `enabled` 过滤，方便管理接口查看和重新启用已停用配置。
        resolver 接入时应在业务策略层决定 disabled Agent 的处理方式。
        """
        stmt = _active_select(AIAgentConfig).where(AIAgentConfig.agent_id == agent_id)
        return await self._scalar_one_or_none(stmt)

    async def get_agent_config_by_id(self, config_id: int) -> AIAgentConfig | None:
        """按数据库主键读取 Agent 配置，供编辑接口使用。"""
        stmt = _active_select(AIAgentConfig).where(AIAgentConfig.id == config_id)
        return await self._scalar_one_or_none(stmt)

    async def list_agent_configs(self) -> list[AIAgentConfig]:
        stmt = _active_select(AIAgentConfig).order_by(AIAgentConfig.agent_id)
        return await self._scalars_all(stmt)

    async def paginate_agent_configs(
        self,
        *,
        page: int,
        size: int,
        keyword: str | None,
    ) -> tuple[list[AIAgentConfig], int]:
        """分页查询 Agent 配置，支持按 agent_id/default_model/policy 模糊搜索。"""
        return await self._paginate(
            model=AIAgentConfig,
            page=page,
            size=size,
            keyword=keyword,
            search_columns=[
                AIAgentConfig.agent_id,
                AIAgentConfig.default_model_key,
                AIAgentConfig.approval_policy_key,
            ],
            order_column=AIAgentConfig.agent_id,
        )

    async def create_agent_config(self, values: dict[str, Any]) -> AIAgentConfig:
        config = AIAgentConfig(**values)
        return await self._add(config)

    async def update_agent_config(self, config: AIAgentConfig, values: dict[str, Any]) -> AIAgentConfig:
        return await self._update(config, values)

    async def get_mcp_server(self, server_key: str) -> AIMCPServer | None:
        """读取 MCP server 配置，不按 enabled 过滤。"""
        stmt = _active_select(AIMCPServer).where(AIMCPServer.server_key == server_key)
        return await self._scalar_one_or_none(stmt)

    async def get_mcp_server_by_id(self, server_id: int) -> AIMCPServer | None:
        """按数据库主键读取 MCP server 配置，供编辑接口使用。"""
        stmt = _active_select(AIMCPServer).where(AIMCPServer.id == server_id)
        return await self._scalar_one_or_none(stmt)

    async def list_mcp_servers(self) -> list[AIMCPServer]:
        stmt = _active_select(AIMCPServer).order_by(AIMCPServer.server_key)
        return await self._scalars_all(stmt)

    async def paginate_mcp_servers(
        self,
        *,
        page: int,
        size: int,
        keyword: str | None,
    ) -> tuple[list[AIMCPServer], int]:
        """分页查询 MCP server，支持按 key/name/transport/url/command 模糊搜索。"""
        return await self._paginate(
            model=AIMCPServer,
            page=page,
            size=size,
            keyword=keyword,
            search_columns=[
                AIMCPServer.server_key,
                AIMCPServer.name,
                AIMCPServer.transport,
                AIMCPServer.url,
                AIMCPServer.command,
            ],
            order_column=AIMCPServer.server_key,
        )

    async def create_mcp_server(self, values: dict[str, Any]) -> AIMCPServer:
        server = AIMCPServer(**values)
        return await self._add(server)

    async def update_mcp_server(self, server: AIMCPServer, values: dict[str, Any]) -> AIMCPServer:
        return await self._update(server, values)

    async def list_agent_mcp_bindings(self, agent_id: str) -> list[AIAgentMCPBinding]:
        stmt = _active_select(AIAgentMCPBinding).where(
            AIAgentMCPBinding.agent_id == agent_id,
            AIAgentMCPBinding.enabled.is_(True),
        ).order_by(AIAgentMCPBinding.server_key)
        return await self._scalars_all(stmt)

    async def paginate_agent_mcp_bindings(
        self,
        *,
        agent_id: str,
        page: int,
        size: int,
        keyword: str | None,
    ) -> tuple[list[AIAgentMCPBinding], int]:
        """分页查询 Agent-MCP 绑定，支持按 server_key 模糊搜索。"""
        keyword_clause = _keyword_clause(keyword, [AIAgentMCPBinding.server_key])
        where_clauses = [
            *_active_clauses(AIAgentMCPBinding),
            AIAgentMCPBinding.agent_id == agent_id,
            AIAgentMCPBinding.enabled.is_(True),
        ]
        if keyword_clause is not None:
            where_clauses.append(keyword_clause)

        offset, limit = page_size(page, size)
        count_stmt = select(func.count()).select_from(AIAgentMCPBinding).where(*where_clauses)
        total = int((await self.session.execute(count_stmt)).scalar_one())
        stmt = (
            select(AIAgentMCPBinding)
            .where(*where_clauses)
            .order_by(AIAgentMCPBinding.server_key)
            .offset(offset)
            .limit(limit)
        )
        return await self._scalars_all(stmt), total

    async def replace_agent_mcp_bindings(
        self,
        agent_id: str,
        binding_values: Sequence[dict[str, Any]],
    ) -> list[AIAgentMCPBinding]:
        """整体替换某个 Agent 的 MCP allowlist。

        使用逻辑删除保留历史痕迹；后续如果接审计表，可以在 service 层记录 before/after。
        """
        current = await self.list_agent_mcp_bindings(agent_id)
        for binding in current:
            binding.is_deleted = 1
            binding.touch()

        new_bindings: list[AIAgentMCPBinding] = []
        for values in binding_values:
            binding = AIAgentMCPBinding(agent_id=agent_id, **values)
            self.session.add(binding)
            new_bindings.append(binding)
        await self.session.flush()
        for binding in new_bindings:
            await self.session.refresh(binding)
        return new_bindings

    async def list_agent_mcp_servers(self, agent_id: str) -> list[AIMCPServer]:
        bindings = await self.list_agent_mcp_bindings(agent_id)
        server_keys = [binding.server_key for binding in bindings]
        if not server_keys:
            return []

        stmt = _active_select(AIMCPServer).where(
            AIMCPServer.server_key.in_(server_keys),
            AIMCPServer.enabled.is_(True),
        )
        servers = await self._scalars_all(stmt)
        return _sort_by_key_order(servers, server_keys)

    async def _scalar_one_or_none(self, stmt: Select) -> object | None:
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def _scalars_all(self, stmt: Select) -> list:
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def _add(self, instance: Any) -> Any:
        self.session.add(instance)
        await self.session.flush()
        await self.session.refresh(instance)
        return instance

    async def _update(self, instance: Any, values: dict[str, Any]) -> Any:
        for key, value in values.items():
            setattr(instance, key, value)
        instance.touch()
        await self.session.flush()
        await self.session.refresh(instance)
        return instance

    async def _paginate(
        self,
        *,
        model: type,
        page: int,
        size: int,
        keyword: str | None,
        search_columns: Sequence[Any],
        order_column: Any,
    ) -> tuple[list[Any], int]:
        where_clauses = _active_clauses(model)
        keyword_clause = _keyword_clause(keyword, search_columns)
        if keyword_clause is not None:
            where_clauses.append(keyword_clause)

        offset, limit = page_size(page, size)
        count_stmt = select(func.count()).select_from(model).where(*where_clauses)
        total = int((await self.session.execute(count_stmt)).scalar_one())
        stmt = select(model).where(*where_clauses).order_by(order_column).offset(offset).limit(limit)
        return await self._scalars_all(stmt), total


def _active_select(model: type) -> Select:
    """统一套上项目通用的逻辑删除和状态过滤。"""
    return select(model).where(*_active_clauses(model))


def _active_clauses(model: type) -> list[Any]:
    return [model.is_deleted == 0, model.status == 1]


def _keyword_clause(keyword: str | None, columns: Sequence[Any]) -> Any | None:
    normalized = (keyword or "").strip()
    if not normalized:
        return None
    return or_(*[column.ilike(f"%{normalized}%") for column in columns])


def _sort_by_key_order(servers: Sequence[AIMCPServer], server_keys: Sequence[str]) -> list[AIMCPServer]:
    """DB 的 IN 查询不保证顺序，这里恢复 binding 声明顺序。"""
    order = {server_key: index for index, server_key in enumerate(server_keys)}
    return sorted(servers, key=lambda item: order.get(item.server_key, len(order)))
