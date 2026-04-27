from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.config_store.schemas import (
    AgentConfigCreate,
    AgentConfigUpdate,
    AgentMCPBindingsReplace,
    AIConfigListQuery,
    AIModelCreate,
    AIModelUpdate,
    MCPServerCreate,
    MCPServerUpdate,
    ModelProviderCreate,
    ModelProviderUpdate,
)
from app.ai.exceptions import AIConfigConflictError, AIConfigNotFoundError, AIConfigValidationError
from app.ai.services.config_admin_service import AIConfigAdminService
from app.core.custom_exception import CustomException
from app.core.response import api_response
from app.db.session import get_db_session

router = APIRouter(prefix="/ai-config", tags=["ai-config"])


def _service(session: AsyncSession = Depends(get_db_session)) -> AIConfigAdminService:
    return AIConfigAdminService(session)


@router.get("/model-providers", summary="查询模型供应商列表")
async def list_model_providers(
    query: AIConfigListQuery = Depends(),
    service: AIConfigAdminService = Depends(_service),
):
    return api_response(data=await service.list_model_providers(query), is_pop=False)


@router.post("/model-providers", summary="创建模型供应商")
async def create_model_provider(payload: ModelProviderCreate, service: AIConfigAdminService = Depends(_service)):
    try:
        result = await service.create_model_provider(payload)
    except AIConfigConflictError as exc:
        raise CustomException(status_code=409, detail=str(exc), custom_code=10003) from exc
    return api_response(http_code=201, code=201, data=result.model_dump(mode="json"))


@router.get("/model-providers/{provider_key}", summary="查询模型供应商详情")
async def get_model_provider(provider_key: str, service: AIConfigAdminService = Depends(_service)):
    try:
        result = await service.get_model_provider(provider_key)
    except AIConfigNotFoundError as exc:
        raise CustomException(status_code=404, detail=str(exc), custom_code=10002) from exc
    return api_response(data=result.model_dump(mode="json"))


@router.put("/model-providers/{provider_id}", summary="更新模型供应商")
async def update_model_provider(
    provider_id: int,
    payload: ModelProviderUpdate,
    service: AIConfigAdminService = Depends(_service),
):
    try:
        result = await service.update_model_provider(provider_id, payload)
    except AIConfigNotFoundError as exc:
        raise CustomException(status_code=404, detail=str(exc), custom_code=10002) from exc
    return api_response(code=203, data=result.model_dump(mode="json"))


@router.get("/models", summary="查询模型列表")
async def list_models(
    query: AIConfigListQuery = Depends(),
    service: AIConfigAdminService = Depends(_service),
):
    return api_response(data=await service.list_models(query), is_pop=False)


@router.post("/models", summary="创建模型配置")
async def create_model(payload: AIModelCreate, service: AIConfigAdminService = Depends(_service)):
    try:
        result = await service.create_model(payload)
    except AIConfigConflictError as exc:
        raise CustomException(status_code=409, detail=str(exc), custom_code=10003) from exc
    except AIConfigValidationError as exc:
        raise CustomException(status_code=400, detail=str(exc), custom_code=10005) from exc
    return api_response(http_code=201, code=201, data=result.model_dump(mode="json"))


@router.get("/models/{model_key}", summary="查询模型详情")
async def get_model(model_key: str, service: AIConfigAdminService = Depends(_service)):
    try:
        result = await service.get_model(model_key)
    except AIConfigNotFoundError as exc:
        raise CustomException(status_code=404, detail=str(exc), custom_code=10002) from exc
    return api_response(data=result.model_dump(mode="json"))


@router.put("/models/{model_id}", summary="更新模型配置")
async def update_model(model_id: int, payload: AIModelUpdate, service: AIConfigAdminService = Depends(_service)):
    try:
        result = await service.update_model(model_id, payload)
    except AIConfigNotFoundError as exc:
        raise CustomException(status_code=404, detail=str(exc), custom_code=10002) from exc
    except AIConfigValidationError as exc:
        raise CustomException(status_code=400, detail=str(exc), custom_code=10005) from exc
    return api_response(code=203, data=result.model_dump(mode="json"))


@router.get("/agents", summary="查询 Agent 配置列表")
async def list_agent_configs(
    query: AIConfigListQuery = Depends(),
    service: AIConfigAdminService = Depends(_service),
):
    return api_response(data=await service.list_agent_configs(query), is_pop=False)


@router.post("/agents", summary="创建 Agent 配置")
async def create_agent_config(payload: AgentConfigCreate, service: AIConfigAdminService = Depends(_service)):
    try:
        result = await service.create_agent_config(payload)
    except AIConfigConflictError as exc:
        raise CustomException(status_code=409, detail=str(exc), custom_code=10003) from exc
    except AIConfigValidationError as exc:
        raise CustomException(status_code=400, detail=str(exc), custom_code=10005) from exc
    return api_response(http_code=201, code=201, data=result.model_dump(mode="json"))


@router.get("/agents/{agent_id}/config", summary="查询 Agent 配置详情")
async def get_agent_config(agent_id: str, service: AIConfigAdminService = Depends(_service)):
    try:
        result = await service.get_agent_config(agent_id)
    except AIConfigNotFoundError as exc:
        raise CustomException(status_code=404, detail=str(exc), custom_code=10002) from exc
    return api_response(data=result.model_dump(mode="json"))


@router.put("/agents/{config_id}/config", summary="更新 Agent 配置")
async def update_agent_config(
    config_id: int,
    payload: AgentConfigUpdate,
    service: AIConfigAdminService = Depends(_service),
):
    try:
        result = await service.update_agent_config(config_id, payload)
    except AIConfigNotFoundError as exc:
        raise CustomException(status_code=404, detail=str(exc), custom_code=10002) from exc
    except AIConfigValidationError as exc:
        raise CustomException(status_code=400, detail=str(exc), custom_code=10005) from exc
    return api_response(code=203, data=result.model_dump(mode="json"))


@router.get("/mcp-servers", summary="查询 MCP Server 列表")
async def list_mcp_servers(
    query: AIConfigListQuery = Depends(),
    service: AIConfigAdminService = Depends(_service),
):
    return api_response(data=await service.list_mcp_servers(query), is_pop=False)


@router.post("/mcp-servers", summary="创建 MCP Server 配置")
async def create_mcp_server(payload: MCPServerCreate, service: AIConfigAdminService = Depends(_service)):
    try:
        result = await service.create_mcp_server(payload)
    except AIConfigConflictError as exc:
        raise CustomException(status_code=409, detail=str(exc), custom_code=10003) from exc
    return api_response(http_code=201, code=201, data=result.model_dump(mode="json"))


@router.get("/mcp-servers/{server_key}", summary="查询 MCP Server 详情")
async def get_mcp_server(server_key: str, service: AIConfigAdminService = Depends(_service)):
    try:
        result = await service.get_mcp_server(server_key)
    except AIConfigNotFoundError as exc:
        raise CustomException(status_code=404, detail=str(exc), custom_code=10002) from exc
    return api_response(data=result.model_dump(mode="json"))


@router.put("/mcp-servers/{server_id}", summary="更新 MCP Server 配置")
async def update_mcp_server(
    server_id: int,
    payload: MCPServerUpdate,
    service: AIConfigAdminService = Depends(_service),
):
    try:
        result = await service.update_mcp_server(server_id, payload)
    except AIConfigNotFoundError as exc:
        raise CustomException(status_code=404, detail=str(exc), custom_code=10002) from exc
    return api_response(code=203, data=result.model_dump(mode="json"))


@router.get("/agents/{agent_id}/mcp-bindings", summary="查询 Agent 的 MCP 绑定列表")
async def list_agent_mcp_bindings(
    agent_id: str,
    query: AIConfigListQuery = Depends(),
    service: AIConfigAdminService = Depends(_service),
):
    return api_response(data=await service.list_agent_mcp_bindings(agent_id, query), is_pop=False)


@router.put("/agents/{agent_id}/mcp-bindings", summary="替换 Agent 的 MCP 绑定列表")
async def replace_agent_mcp_bindings(
    agent_id: str,
    payload: AgentMCPBindingsReplace,
    service: AIConfigAdminService = Depends(_service),
):
    try:
        result = await service.replace_agent_mcp_bindings(agent_id, payload.bindings)
    except AIConfigValidationError as exc:
        raise CustomException(status_code=400, detail=str(exc), custom_code=10005) from exc
    return api_response(code=203, data=[item.model_dump(mode="json") for item in result])
