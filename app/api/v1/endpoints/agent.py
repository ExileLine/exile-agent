from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from app.ai.deps import RequestContext
from app.ai.exceptions import AIDisabledError, AgentNotFoundError, AIRuntimeError
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


@router.post("/chat", summary="执行 Agent 对话")
async def chat_with_agent(payload: AgentChatRequest, request: Request):
    service = _build_chat_service(request)
    request_context = RequestContext(
        request_id=getattr(request.state, "request_id", None) or request.headers.get("x-request-id", ""),
        user_id=request.headers.get("x-user-id"),
        session_id=payload.session_id,
    )

    try:
        result = await service.chat(request_context=request_context, payload=payload)
    except AgentNotFoundError as exc:
        raise CustomException(status_code=404, detail=str(exc), custom_code=404) from exc
    except AIDisabledError as exc:
        raise CustomException(status_code=503, detail=str(exc), custom_code=503) from exc
    except AIRuntimeError as exc:
        raise CustomException(status_code=500, detail=str(exc), custom_code=500) from exc

    return api_response(data=result.model_dump(mode="json"))


@router.post("/chat/stream", summary="执行 Agent 流式对话")
async def stream_agent_chat(payload: AgentChatRequest, request: Request):
    service = _build_chat_service(request)
    request_context = RequestContext(
        request_id=getattr(request.state, "request_id", None) or request.headers.get("x-request-id", ""),
        user_id=request.headers.get("x-user-id"),
        session_id=payload.session_id,
    )

    try:
        event_iterator = service.stream(request_context=request_context, payload=payload)
    except AgentNotFoundError as exc:
        raise CustomException(status_code=404, detail=str(exc), custom_code=404) from exc
    except AIDisabledError as exc:
        raise CustomException(status_code=503, detail=str(exc), custom_code=503) from exc
    except AIRuntimeError as exc:
        raise CustomException(status_code=500, detail=str(exc), custom_code=500) from exc

    return StreamingResponse(event_iterator, media_type="text/event-stream")


@router.post("/chat/resume", summary="继续执行待审批的 Agent 对话")
async def resume_agent_chat(payload: AgentChatResumeRequest, request: Request):
    service = _build_chat_service(request)
    request_context = RequestContext(
        request_id=getattr(request.state, "request_id", None) or request.headers.get("x-request-id", ""),
        user_id=request.headers.get("x-user-id"),
        session_id=payload.session_id,
    )

    try:
        result = await service.resume(request_context=request_context, payload=payload)
    except AgentNotFoundError as exc:
        raise CustomException(status_code=404, detail=str(exc), custom_code=404) from exc
    except AIDisabledError as exc:
        raise CustomException(status_code=503, detail=str(exc), custom_code=503) from exc
    except AIRuntimeError as exc:
        raise CustomException(status_code=500, detail=str(exc), custom_code=500) from exc

    return api_response(data=result.model_dump(mode="json"))
