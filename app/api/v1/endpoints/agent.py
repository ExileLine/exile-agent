import tempfile
from pathlib import Path

from fastapi import APIRouter, Query, Request
from fastapi.responses import FileResponse, StreamingResponse

from app.ai.deps import RequestContext
from app.ai.exceptions import (
    AIDisabledError,
    AgentNotFoundError,
    AIConfigNotFoundError,
    AIConfigValidationError,
    AIRuntimeError,
    MCPConfigurationError,
    MCPRuntimeError,
    MCPServerNotFoundError,
    SkillConfigurationError,
    SkillNotFoundError,
)
from app.ai.schemas.chat import AgentChatRequest, AgentChatResumeRequest
from app.ai.services import ChatService
from app.core.custom_exception import CustomException
from app.core.response import api_response

router = APIRouter(prefix="/agents", tags=["agents"])


def _build_chat_service(request: Request) -> ChatService:
    runner = getattr(request.app.state, "ai_runner", None)
    agent_manager = getattr(request.app.state, "ai_agent_manager", None)
    skill_registry = getattr(request.app.state, "ai_skill_registry", None)
    if runner is None or agent_manager is None:
        raise CustomException(status_code=503, detail="AI runtime 未初始化", custom_code=503)
    return ChatService(runner=runner, agent_manager=agent_manager, skill_registry=skill_registry)


def _raise_agent_api_exception(exc: AIRuntimeError) -> None:
    """把 AI runtime 异常映射成对前端更明确的 HTTP 响应。"""

    if isinstance(exc, (AIConfigValidationError, MCPConfigurationError, SkillConfigurationError)):
        raise CustomException(status_code=400, detail=str(exc), custom_code=10005) from exc

    if isinstance(exc, (AgentNotFoundError, AIConfigNotFoundError, MCPServerNotFoundError, SkillNotFoundError)):
        raise CustomException(status_code=404, detail=str(exc), custom_code=10002) from exc

    if isinstance(exc, AIDisabledError):
        raise CustomException(status_code=503, detail=str(exc), custom_code=503) from exc

    if isinstance(exc, MCPRuntimeError):
        raise CustomException(status_code=502, detail=str(exc), custom_code=502) from exc

    raise CustomException(status_code=500, detail=str(exc), custom_code=500) from exc


@router.get("", summary="查询已注册的 Agent 列表")
async def list_agents(request: Request):
    service = _build_chat_service(request)
    return api_response(
        data=[item.model_dump(mode="json") for item in service.list_agents()],
        is_pop=False,
    )


@router.get("/skills", summary="查询已注册的 Skill 列表")
async def list_skills(request: Request):
    service = _build_chat_service(request)
    return api_response(data=service.list_skills(), is_pop=False)


@router.get("/team-runs/{team_run_id}", summary="查询多 Agent 执行轨迹")
async def get_team_run_trace(team_run_id: str, request: Request):
    service = _build_chat_service(request)
    result = await service.get_team_run_trace(team_run_id)
    if result is None:
        raise CustomException(status_code=404, detail=f"未找到 Team Run Trace: {team_run_id}", custom_code=10002)
    return api_response(data=result)


@router.get("/artifacts/{request_id}/{filename}/download", summary="下载 Agent 生成产物")
async def download_agent_artifact(request_id: str, filename: str):
    artifacts_root = Path(tempfile.gettempdir()).resolve() / "exile-agent-skill-runs"
    artifact_path = (artifacts_root / request_id / filename).resolve()
    if artifacts_root not in artifact_path.parents:
        raise CustomException(status_code=400, detail="非法产物路径", custom_code=10005)
    if not artifact_path.exists() or not artifact_path.is_file():
        raise CustomException(status_code=404, detail="产物不存在或已过期", custom_code=10002)
    return FileResponse(
        path=str(artifact_path),
        filename=artifact_path.name,
        media_type="application/octet-stream",
    )


@router.get("/users/{user_id}/sessions/histories", summary="按用户分页查询历史会话列表")
async def list_user_session_histories(
    user_id: str,
    request: Request,
    page: int = Query(default=1, ge=1, description="页码"),
    size: int = Query(default=20, ge=1, le=200, description="每页数量"),
    agent_ids: str | None = None,
):
    service = _build_chat_service(request)
    request_context = RequestContext(
        request_id=getattr(request.state, "request_id", None) or request.headers.get("x-request-id", ""),
        user_id=user_id,
        tenant_id=request.headers.get("x-tenant-id"),
    )
    parsed_agent_ids = [item.strip() for item in agent_ids.split(",")] if agent_ids else None
    return api_response(
        data=await service.get_user_session_histories(
            request_context=request_context,
            user_id=user_id,
            page=page,
            size=size,
            agent_ids=parsed_agent_ids,
        )
    )


@router.get("/sessions/{session_id}/histories", summary="查询 Agent 会话历史")
async def get_session_histories(
    session_id: str,
    request: Request,
    agent_ids: str | None = None,
    merge: bool = False,
):
    service = _build_chat_service(request)
    request_context = RequestContext(
        request_id=getattr(request.state, "request_id", None) or request.headers.get("x-request-id", ""),
        user_id=request.headers.get("x-user-id"),
        tenant_id=request.headers.get("x-tenant-id"),
        session_id=session_id,
    )
    parsed_agent_ids = [item.strip() for item in agent_ids.split(",")] if agent_ids else None
    return api_response(
        data=await service.get_session_histories(
            request_context=request_context,
            session_id=session_id,
            agent_ids=parsed_agent_ids,
            merge=merge,
        )
    )


@router.post("/chat", summary="执行 Agent 对话")
async def chat_with_agent(payload: AgentChatRequest, request: Request):
    service = _build_chat_service(request)
    request_context = RequestContext(
        request_id=getattr(request.state, "request_id", None) or request.headers.get("x-request-id", ""),
        user_id=request.headers.get("x-user-id"),
        tenant_id=request.headers.get("x-tenant-id"),
        session_id=payload.session_id,
    )

    try:
        result = await service.chat(request_context=request_context, payload=payload)
    except AIRuntimeError as exc:
        _raise_agent_api_exception(exc)

    return api_response(data=result.model_dump(mode="json"))


@router.post("/chat/stream", summary="执行 Agent 流式对话")
async def stream_agent_chat(payload: AgentChatRequest, request: Request):
    service = _build_chat_service(request)
    request_context = RequestContext(
        request_id=getattr(request.state, "request_id", None) or request.headers.get("x-request-id", ""),
        user_id=request.headers.get("x-user-id"),
        tenant_id=request.headers.get("x-tenant-id"),
        session_id=payload.session_id,
    )

    try:
        event_iterator = service.stream(request_context=request_context, payload=payload)
    except AIRuntimeError as exc:
        _raise_agent_api_exception(exc)

    return StreamingResponse(event_iterator, media_type="text/event-stream")


@router.post("/chat/resume", summary="继续执行待审批的 Agent 对话")
async def resume_agent_chat(payload: AgentChatResumeRequest, request: Request):
    service = _build_chat_service(request)
    request_context = RequestContext(
        request_id=getattr(request.state, "request_id", None) or request.headers.get("x-request-id", ""),
        user_id=request.headers.get("x-user-id"),
        tenant_id=request.headers.get("x-tenant-id"),
        session_id=payload.session_id,
    )

    try:
        result = await service.resume(request_context=request_context, payload=payload)
    except AIRuntimeError as exc:
        _raise_agent_api_exception(exc)

    return api_response(data=result.model_dump(mode="json"))
