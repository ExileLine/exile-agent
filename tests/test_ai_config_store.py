import asyncio
from typing import Any

from sqlalchemy.sql import Select

from app.ai.config_store import (
    AIAgentMCPBinding,
    AIMCPServer,
    AIConfigRepository,
    AIModel,
    AIModelProvider,
)
from app.ai.config_store.encryption import decrypt_secret
from app.ai.config_store.schemas import MCPServerCreate, ModelProviderCreate
from app.ai.services.config_admin_service import (
    _mcp_create_values,
    _mcp_server_to_read,
    _provider_create_values,
    _provider_to_read,
)
from app.main import app


class _FakeScalars:
    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows

    def all(self) -> list[Any]:
        return self.rows


class _FakeResult:
    def __init__(self, *, row: Any = None, rows: list[Any] | None = None) -> None:
        self.row = row
        self.rows = rows or []

    def scalar_one_or_none(self) -> Any:
        return self.row

    def scalar_one(self) -> Any:
        return self.row

    def scalars(self) -> _FakeScalars:
        return _FakeScalars(self.rows)


class _FakeSession:
    def __init__(self, results: list[_FakeResult]) -> None:
        self.results = results
        self.statements: list[Select] = []

    async def execute(self, stmt: Select) -> _FakeResult:
        self.statements.append(stmt)
        return self.results.pop(0)


def _compile_sql(stmt: Select) -> str:
    return str(stmt.compile(compile_kwargs={"literal_binds": True}))


def test_config_store_models_use_expected_table_names_and_secret_columns() -> None:
    assert AIModelProvider.__tablename__ == "ai_model_provider"
    assert AIModel.__tablename__ == "ai_model"
    assert AIMCPServer.__tablename__ == "ai_mcp_server"
    assert AIAgentMCPBinding.__tablename__ == "ai_agent_mcp_binding"

    provider_columns = set(AIModelProvider.__table__.columns.keys())
    assert "api_key_encrypted" in provider_columns
    assert "api_key" not in provider_columns

    mcp_columns = set(AIMCPServer.__table__.columns.keys())
    assert "headers_encrypted_json" in mcp_columns
    assert "env_encrypted_json" in mcp_columns
    assert "headers" not in mcp_columns
    assert "env" not in mcp_columns


def test_repository_get_enabled_model_builds_active_filtered_query() -> None:
    async def run() -> None:
        session = _FakeSession([_FakeResult(row=None)])
        repository = AIConfigRepository(session)  # type: ignore[arg-type]

        result = await repository.get_enabled_model("deepseek-chat")

        assert result is None
        sql = _compile_sql(session.statements[0])
        assert "ai_model.model_key = 'deepseek-chat'" in sql
        assert "ai_model.enabled IS true" in sql
        assert "ai_model.is_deleted = 0" in sql
        assert "ai_model.status = 1" in sql

    asyncio.run(run())


def test_repository_list_agent_mcp_servers_preserves_binding_order() -> None:
    async def run() -> None:
        binding_a = AIAgentMCPBinding(agent_id="chat-agent", server_key="filesystem")
        binding_b = AIAgentMCPBinding(agent_id="chat-agent", server_key="docs")
        server_b = AIMCPServer(server_key="docs", name="Docs", transport="streamable-http", url="https://example.com/mcp")
        server_a = AIMCPServer(server_key="filesystem", name="Filesystem", transport="stdio", command="uvx")

        session = _FakeSession(
            [
                _FakeResult(rows=[binding_a, binding_b]),
                _FakeResult(rows=[server_b, server_a]),
            ]
        )
        repository = AIConfigRepository(session)  # type: ignore[arg-type]

        servers = await repository.list_agent_mcp_servers("chat-agent")

        assert [server.server_key for server in servers] == ["filesystem", "docs"]
        assert len(session.statements) == 2
        binding_sql = _compile_sql(session.statements[0])
        assert "ai_agent_mcp_binding.agent_id = 'chat-agent'" in binding_sql
        assert "ai_agent_mcp_binding.enabled IS true" in binding_sql

    asyncio.run(run())


def test_repository_paginate_model_providers_applies_keyword_and_page() -> None:
    async def run() -> None:
        provider = AIModelProvider(
            provider_key="deepseek",
            name="DeepSeek",
            provider_type="openai_compatible",
            enabled=True,
        )
        session = _FakeSession([_FakeResult(row=1), _FakeResult(rows=[provider])])
        repository = AIConfigRepository(session)  # type: ignore[arg-type]

        records, total = await repository.paginate_model_providers(page=2, size=10, keyword="deep")

        assert total == 1
        assert records == [provider]
        count_sql = _compile_sql(session.statements[0])
        page_sql = _compile_sql(session.statements[1])
        assert "lower(ai_model_provider.provider_key) LIKE lower('%deep%')" in count_sql
        assert "ai_model_provider.is_deleted = 0" in count_sql
        assert "LIMIT 10" in page_sql
        assert "OFFSET 10" in page_sql

    asyncio.run(run())


def test_model_provider_secret_is_encrypted_and_masked_on_read() -> None:
    values = _provider_create_values(
        ModelProviderCreate(
            provider_key="deepseek",
            name="DeepSeek",
            provider_type="openai_compatible",
            api_key="secret-token",
        )
    )

    assert values["api_key_encrypted"] != "secret-token"
    assert decrypt_secret(values["api_key_encrypted"]) == "secret-token"

    provider = AIModelProvider(
        provider_key="deepseek",
        name="DeepSeek",
        provider_type="openai_compatible",
        api_key_encrypted=values["api_key_encrypted"],
        enabled=True,
    )
    read_model = _provider_to_read(provider)

    assert read_model.has_api_key is True
    assert "id" in read_model.model_dump()
    assert "api_key" not in read_model.model_dump()
    assert "api_key_encrypted" not in read_model.model_dump()


def test_mcp_server_secret_mappings_are_encrypted_and_read_model_masks_values() -> None:
    values = _mcp_create_values(
        MCPServerCreate(
            server_key="docs",
            name="Docs",
            transport="streamable-http",
            url="https://example.com/mcp",
            headers={"authorization": "Bearer token"},
            env={"API_TOKEN": "env-token"},
        )
    )

    assert values["headers_encrypted_json"]["authorization"] != "Bearer token"
    assert values["env_encrypted_json"]["API_TOKEN"] != "env-token"

    server = AIMCPServer(
        server_key="docs",
        name="Docs",
        transport="streamable-http",
        url="https://example.com/mcp",
        headers_encrypted_json=values["headers_encrypted_json"],
        env_encrypted_json=values["env_encrypted_json"],
        enabled=True,
        auto_route_enabled=True,
        include_instructions=False,
        risk_level="low",
    )
    read_model = _mcp_server_to_read(server)

    payload = read_model.model_dump()
    assert "id" in payload
    assert payload["header_keys"] == ["authorization"]
    assert payload["env_keys"] == ["API_TOKEN"]
    assert "headers" not in payload
    assert "env" not in payload
    assert "headers_encrypted_json" not in payload
    assert "env_encrypted_json" not in payload


def test_ai_config_update_routes_use_put_and_database_ids() -> None:
    schema = app.openapi()

    assert "put" in schema["paths"]["/api/v1/ai-config/model-providers/{provider_id}"]
    assert "patch" not in schema["paths"]["/api/v1/ai-config/model-providers/{provider_id}"]
    assert "put" in schema["paths"]["/api/v1/ai-config/models/{model_id}"]
    assert "put" in schema["paths"]["/api/v1/ai-config/agents/{config_id}/config"]
    assert "put" in schema["paths"]["/api/v1/ai-config/mcp-servers/{server_id}"]


def test_ai_config_list_routes_expose_pagination_and_keyword_query_params() -> None:
    schema = app.openapi()
    list_paths = [
        "/api/v1/ai-config/model-providers",
        "/api/v1/ai-config/models",
        "/api/v1/ai-config/agents",
        "/api/v1/ai-config/mcp-servers",
        "/api/v1/ai-config/agents/{agent_id}/mcp-bindings",
    ]

    for path in list_paths:
        parameters = schema["paths"][path]["get"]["parameters"]
        param_names = {item["name"] for item in parameters}
        assert {"page", "size", "keyword"}.issubset(param_names)
