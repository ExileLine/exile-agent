from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from loguru import logger
from sqlalchemy import Float, Index, JSON, String, Text, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import CustomBaseModel


class AITeamRunTrace(CustomBaseModel):
    """多 Agent Team 一次协同运行的主记录。"""

    __table_name__ = "ai_team_run_trace"
    __table_args__ = (
        Index("ix_ai_team_run_trace_team_run_id", "team_run_id", unique=True),
        Index("ix_ai_team_run_trace_session_user", "session_id", "user_id", "tenant_id"),
    )

    team_run_id: Mapped[str] = mapped_column(String(64), nullable=False, comment="Team Run ID")
    run_kind: Mapped[str] = mapped_column(String(32), nullable=False, comment="运行类型：chat/stream")
    agent_id: Mapped[str] = mapped_column(String(64), nullable=False, comment="Team Agent ID")
    request_id: Mapped[str] = mapped_column(String(64), nullable=False, comment="请求 ID")
    session_id: Mapped[str | None] = mapped_column(String(128), nullable=True, comment="会话 ID")
    user_id: Mapped[str | None] = mapped_column(String(128), nullable=True, comment="用户 ID")
    tenant_id: Mapped[str | None] = mapped_column(String(128), nullable=True, comment="租户 ID")
    status_text: Mapped[str] = mapped_column(String(32), nullable=False, comment="运行状态")
    model: Mapped[str] = mapped_column(String(128), nullable=False, comment="聚合 Agent 实际模型")
    aggregator_agent_id: Mapped[str] = mapped_column(String(64), nullable=False, comment="聚合 Agent ID")
    worker_agent_ids_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list, comment="Worker Agent ID 列表")
    route_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict, comment="AgentRouter 命中信息")
    usage_json: Mapped[dict | None] = mapped_column(JSON, nullable=True, comment="聚合 Agent usage")
    final_message: Mapped[str | None] = mapped_column(Text, nullable=True, comment="最终聚合输出")
    duration_ms: Mapped[float | None] = mapped_column(Float, nullable=True, comment="总耗时毫秒")
    error: Mapped[str | None] = mapped_column(Text, nullable=True, comment="失败原因")
    metadata_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict, comment="扩展元数据")


class AITeamWorkerRunTrace(CustomBaseModel):
    """多 Agent Team 中单个 Worker 的运行记录。"""

    __table_name__ = "ai_team_worker_run_trace"
    __table_args__ = (
        Index("ix_ai_team_worker_run_trace_team_run", "team_run_id"),
        Index("ix_ai_team_worker_run_trace_worker", "worker_agent_id"),
    )

    team_run_id: Mapped[str] = mapped_column(String(64), nullable=False, comment="所属 Team Run ID")
    worker_agent_id: Mapped[str] = mapped_column(String(64), nullable=False, comment="Worker Agent ID")
    role: Mapped[str] = mapped_column(String(64), nullable=False, comment="Worker 角色")
    status_text: Mapped[str] = mapped_column(String(32), nullable=False, comment="运行状态")
    model: Mapped[str] = mapped_column(String(128), nullable=False, comment="Worker 实际模型")
    message: Mapped[str | None] = mapped_column(Text, nullable=True, comment="Worker 输出")
    error: Mapped[str | None] = mapped_column(Text, nullable=True, comment="错误信息")
    error_type: Mapped[str | None] = mapped_column(String(128), nullable=True, comment="错误类型")
    duration_ms: Mapped[float | None] = mapped_column(Float, nullable=True, comment="耗时毫秒")
    context_injected: Mapped[bool] = mapped_column(default=False, nullable=False, comment="是否注入团队上下文")
    usage_json: Mapped[dict | None] = mapped_column(JSON, nullable=True, comment="Worker usage")
    mcp_servers_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list, comment="实际装配的 MCP server")
    skills_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list, comment="实际装配的 Skills")
    metadata_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict, comment="扩展元数据")


@dataclass(slots=True)
class TeamRunTracePayload:
    team_run_id: str
    run_kind: str
    agent_id: str
    request_id: str
    session_id: str | None
    user_id: str | None
    tenant_id: str | None
    status: str
    model: str
    aggregator_agent_id: str
    worker_agent_ids: list[str]
    route: dict[str, Any]
    usage: dict[str, Any] | None
    final_message: str | None
    duration_ms: float | None
    error: str | None = None
    metadata: dict[str, Any] | None = None
    worker_results: list[dict[str, Any]] | None = None


class TeamRunTraceStore:
    """Team Run Trace 存储。

    DB 可用时写入 `ai_team_run_trace` / `ai_team_worker_run_trace`；
    未初始化 DB 的测试或本地最小链路退化到内存，保证 chat 主链路不被观测能力拖垮。
    """

    def __init__(
        self,
        *,
        db_session_factory: async_sessionmaker[AsyncSession] | Callable[[], AsyncSession] | None,
        db_enabled: bool,
    ) -> None:
        self.db_session_factory = db_session_factory
        self.db_enabled = db_enabled
        self._memory_store: dict[str, dict[str, Any]] = {}

    async def save(self, payload: TeamRunTracePayload) -> None:
        trace = self._payload_to_dict(payload)
        self._memory_store[payload.team_run_id] = trace
        if not self.db_enabled or self.db_session_factory is None:
            return

        try:
            async with self.db_session_factory() as session:
                run = AITeamRunTrace(
                    team_run_id=payload.team_run_id,
                    run_kind=payload.run_kind,
                    agent_id=payload.agent_id,
                    request_id=payload.request_id,
                    session_id=payload.session_id,
                    user_id=payload.user_id,
                    tenant_id=payload.tenant_id,
                    status_text=payload.status,
                    model=payload.model,
                    aggregator_agent_id=payload.aggregator_agent_id,
                    worker_agent_ids_json=payload.worker_agent_ids,
                    route_json=payload.route,
                    usage_json=payload.usage,
                    final_message=payload.final_message,
                    duration_ms=payload.duration_ms,
                    error=payload.error,
                    metadata_json=payload.metadata or {},
                )
                session.add(run)
                for worker in payload.worker_results or []:
                    session.add(_worker_result_to_model(payload.team_run_id, worker))
                await session.commit()
        except Exception:
            logger.exception("保存 Team Run Trace 失败，已保留内存副本: {}", payload.team_run_id)

    async def get(self, team_run_id: str) -> dict[str, Any] | None:
        if self.db_enabled and self.db_session_factory is not None:
            try:
                async with self.db_session_factory() as session:
                    run = await _get_run(session, team_run_id)
                    if run is not None:
                        workers = await _list_workers(session, team_run_id)
                        return _models_to_dict(run, workers)
            except Exception:
                logger.exception("查询 Team Run Trace 失败，尝试读取内存副本: {}", team_run_id)
        return self._memory_store.get(team_run_id)

    @staticmethod
    def _payload_to_dict(payload: TeamRunTracePayload) -> dict[str, Any]:
        return {
            "team_run_id": payload.team_run_id,
            "run_kind": payload.run_kind,
            "agent_id": payload.agent_id,
            "request_id": payload.request_id,
            "session_id": payload.session_id,
            "user_id": payload.user_id,
            "tenant_id": payload.tenant_id,
            "status": payload.status,
            "model": payload.model,
            "aggregator_agent_id": payload.aggregator_agent_id,
            "worker_agent_ids": payload.worker_agent_ids,
            "route": payload.route,
            "usage": payload.usage,
            "final_message": payload.final_message,
            "duration_ms": payload.duration_ms,
            "error": payload.error,
            "metadata": payload.metadata or {},
            "workers": payload.worker_results or [],
        }


async def _get_run(session: AsyncSession, team_run_id: str) -> AITeamRunTrace | None:
    result = await session.execute(
        select(AITeamRunTrace).where(
            AITeamRunTrace.team_run_id == team_run_id,
            AITeamRunTrace.is_deleted == 0,
            AITeamRunTrace.status == 1,
        )
    )
    return result.scalar_one_or_none()


async def _list_workers(session: AsyncSession, team_run_id: str) -> list[AITeamWorkerRunTrace]:
    result = await session.execute(
        select(AITeamWorkerRunTrace)
        .where(
            AITeamWorkerRunTrace.team_run_id == team_run_id,
            AITeamWorkerRunTrace.is_deleted == 0,
            AITeamWorkerRunTrace.status == 1,
        )
        .order_by(AITeamWorkerRunTrace.id)
    )
    return list(result.scalars().all())


def _worker_result_to_model(team_run_id: str, worker: dict[str, Any]) -> AITeamWorkerRunTrace:
    return AITeamWorkerRunTrace(
        team_run_id=team_run_id,
        worker_agent_id=str(worker.get("agent_id") or ""),
        role=str(worker.get("role") or ""),
        status_text=str(worker.get("status") or ""),
        model=str(worker.get("model") or ""),
        message=worker.get("message"),
        error=worker.get("error"),
        error_type=worker.get("error_type"),
        duration_ms=worker.get("duration_ms"),
        context_injected=bool(worker.get("context_injected")),
        usage_json=worker.get("usage"),
        mcp_servers_json=list(worker.get("mcp_servers") or []),
        skills_json=list(worker.get("skills") or []),
        metadata_json={},
    )


def _models_to_dict(run: AITeamRunTrace, workers: list[AITeamWorkerRunTrace]) -> dict[str, Any]:
    return {
        "team_run_id": run.team_run_id,
        "run_kind": run.run_kind,
        "agent_id": run.agent_id,
        "request_id": run.request_id,
        "session_id": run.session_id,
        "user_id": run.user_id,
        "tenant_id": run.tenant_id,
        "status": run.status_text,
        "model": run.model,
        "aggregator_agent_id": run.aggregator_agent_id,
        "worker_agent_ids": run.worker_agent_ids_json or [],
        "route": run.route_json or {},
        "usage": run.usage_json,
        "final_message": run.final_message,
        "duration_ms": run.duration_ms,
        "error": run.error,
        "metadata": run.metadata_json or {},
        "workers": [
            {
                "agent_id": item.worker_agent_id,
                "role": item.role,
                "status": item.status_text,
                "model": item.model,
                "message": item.message,
                "error": item.error,
                "error_type": item.error_type,
                "duration_ms": item.duration_ms,
                "context_injected": item.context_injected,
                "usage": item.usage_json,
                "mcp_servers": item.mcp_servers_json or [],
                "skills": item.skills_json or [],
            }
            for item in workers
        ],
    }
