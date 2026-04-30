from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import shortuuid
from redis.asyncio import Redis

from app.ai.exceptions import AIConfigValidationError


ApprovalStatus = Literal["pending", "completed", "expired"]


@dataclass(slots=True)
class ApprovalRecord:
    """服务端保存的一次待审批 run 快照。"""

    approval_id: str
    run_id: str
    agent_id: str
    request_id: str
    session_id: str | None
    user_id: str | None
    message_history_json: str
    approval_tool_call_ids: list[str]
    call_tool_call_ids: list[str]
    status: ApprovalStatus
    expires_at: datetime
    created_at: datetime
    completed_at: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class ApprovalStore:
    """待审批 run 的服务端存储。

    当前优先使用 Redis；没有 Redis 时退化到内存，便于测试和本地开发。
    """

    def __init__(
        self,
        *,
        redis: Redis | None,
        ttl_seconds: int = 1800,
        key_prefix: str = "ai:approval",
    ) -> None:
        self.redis = redis
        self.ttl_seconds = ttl_seconds
        self.key_prefix = key_prefix
        self._memory_store: dict[str, str] = {}

    async def create(
        self,
        *,
        run_id: str,
        agent_id: str,
        request_id: str,
        session_id: str | None,
        user_id: str | None,
        message_history_json: str,
        approval_tool_call_ids: list[str],
        call_tool_call_ids: list[str],
        metadata: dict[str, Any] | None = None,
    ) -> ApprovalRecord:
        now = datetime.now(UTC)
        record = ApprovalRecord(
            approval_id=shortuuid.uuid(),
            run_id=run_id,
            agent_id=agent_id,
            request_id=request_id,
            session_id=session_id,
            user_id=user_id,
            message_history_json=message_history_json,
            approval_tool_call_ids=approval_tool_call_ids,
            call_tool_call_ids=call_tool_call_ids,
            status="pending",
            expires_at=now + timedelta(seconds=self.ttl_seconds),
            created_at=now,
            metadata=metadata or {},
        )
        await self._save(record)
        return record

    async def get_pending(
        self,
        approval_id: str,
        *,
        agent_id: str | None,
        session_id: str | None,
        user_id: str | None,
    ) -> ApprovalRecord:
        record = await self.get(approval_id)
        if record is None:
            raise AIConfigValidationError(f"审批单不存在: {approval_id}")
        if record.expires_at <= datetime.now(UTC):
            record.status = "expired"
            await self._save(record)
            raise AIConfigValidationError(f"审批单已过期: {approval_id}")
        if record.status != "pending":
            raise AIConfigValidationError(f"审批单状态不可续跑: {record.status}")
        if agent_id is not None and agent_id != record.agent_id:
            raise AIConfigValidationError("审批单 Agent 不匹配")
        if session_id is not None and session_id != record.session_id:
            raise AIConfigValidationError("审批单 session 不匹配")
        if record.user_id is not None and user_id != record.user_id:
            raise AIConfigValidationError("审批单用户不匹配")
        return record

    async def get(self, approval_id: str) -> ApprovalRecord | None:
        payload = await self._get_payload(approval_id)
        if not payload:
            return None
        return _record_from_json(payload)

    async def mark_completed(self, approval_id: str) -> None:
        record = await self.get(approval_id)
        if record is None:
            return
        record.status = "completed"
        record.completed_at = datetime.now(UTC)
        await self._save(record)

    async def _save(self, record: ApprovalRecord) -> None:
        payload = _record_to_json(record)
        ttl_seconds = max(1, int((record.expires_at - datetime.now(UTC)).total_seconds()))
        if self.redis is not None:
            await self.redis.set(self._build_key(record.approval_id), payload, ex=ttl_seconds)
            return
        self._memory_store[record.approval_id] = payload

    async def _get_payload(self, approval_id: str) -> str | bytes | None:
        if self.redis is not None:
            return await self.redis.get(self._build_key(approval_id))
        return self._memory_store.get(approval_id)

    def _build_key(self, approval_id: str) -> str:
        return f"{self.key_prefix}:{approval_id}"


def _record_to_json(record: ApprovalRecord) -> str:
    payload = asdict(record)
    for key in ("expires_at", "created_at", "completed_at"):
        value = payload.get(key)
        if isinstance(value, datetime):
            payload[key] = value.isoformat()
    return json.dumps(payload, ensure_ascii=False)


def _record_from_json(payload: str | bytes) -> ApprovalRecord:
    if isinstance(payload, bytes):
        payload = payload.decode()
    data = json.loads(payload)
    for key in ("expires_at", "created_at", "completed_at"):
        value = data.get(key)
        if isinstance(value, str):
            data[key] = datetime.fromisoformat(value)
    return ApprovalRecord(**data)
