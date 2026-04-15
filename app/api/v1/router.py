from fastapi import APIRouter

from app.api.v1.endpoints.agent import router as agent_router
from app.api.v1.endpoints.health import router as health_router

api_router = APIRouter(prefix="/v1")
api_router.include_router(health_router)
api_router.include_router(agent_router)
