from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from pydantic_ai import ModelMessagesTypeAdapter
from pydantic_ai.messages import ModelMessage
from redis.asyncio import Redis

from app.ai.deps import RequestContext


@dataclass(slots=True)
class HistoryScope:
    """用于隔离会话历史的稳定维度。"""

    session_id: str
    agent_id: str | None = None
    user_id: str | None = None
    tenant_id: str | None = None


@dataclass(slots=True)
class HistoryMetadata:
    """一次会话历史快照的治理元信息。"""

    session_id: str
    agent_id: str | None
    user_id: str | None
    tenant_id: str | None
    model: str | None = None
    skills: list[str] = field(default_factory=list)
    mcp_servers: list[str] = field(default_factory=list)
    usage: dict[str, Any] | None = None
    message_count: int = 0
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(slots=True)
class HistoryRecord:
    """持久化到 Redis/内存中的完整会话历史快照。"""

    messages: list[ModelMessage]
    metadata: HistoryMetadata


class SessionHistoryStore:
    """会话历史存储。

    当前优先使用 Redis 做持久化；
    如果运行环境里没有 Redis 连接，则退化到进程内存存储，
    这样测试环境和本地最小调试链路也能先跑通。
    """

    def __init__(
        self,
        *,
        redis: Redis | None,
        ttl_seconds: int | None,
        key_prefix: str = "session",
        max_messages: int | None = 40,
    ) -> None:
        self.redis = redis
        self.ttl_seconds = ttl_seconds
        self.key_prefix = key_prefix
        self.max_messages = max_messages
        self._memory_store: dict[str, str] = {}

    async def load_messages(
        self,
        session_id: str | None,
        *,
        request_context: RequestContext | None = None,
        agent_id: str | None = None,
    ) -> list[ModelMessage]:
        """加载某个会话当前保存的完整 message history。"""

        record = await self.load_record(
            session_id,
            request_context=request_context,
            agent_id=agent_id,
        )
        return record.messages if record is not None else []

    async def load_record(
        self,
        session_id: str | None,
        *,
        request_context: RequestContext | None = None,
        agent_id: str | None = None,
    ) -> HistoryRecord | None:
        """加载会话历史和对应 metadata。"""

        scope = self._build_scope(session_id, request_context=request_context, agent_id=agent_id)
        if scope is None:
            return None

        payload = await self._get_payload(scope)
        if not payload:
            return None
        return self._record_from_payload(payload, scope)

    async def save_messages(
        self,
        session_id: str | None,
        messages: Sequence[ModelMessage],
        *,
        request_context: RequestContext | None = None,
        agent_id: str | None = None,
        model: str | None = None,
        skills: Sequence[str] = (),
        mcp_servers: Sequence[str] = (),
        usage: dict[str, Any] | None = None,
    ) -> None:
        """覆盖写入某个会话的完整 message history。"""

        scope = self._build_scope(session_id, request_context=request_context, agent_id=agent_id)
        if scope is None:
            return

        existing = await self.load_record(
            session_id,
            request_context=request_context,
            agent_id=agent_id,
        )
        now = datetime.now(UTC)
        trimmed_messages = self._trim_messages(list(messages))
        metadata = HistoryMetadata(
            session_id=scope.session_id,
            agent_id=scope.agent_id,
            user_id=scope.user_id,
            tenant_id=scope.tenant_id,
            model=model,
            skills=list(skills),
            mcp_servers=list(mcp_servers),
            usage=usage,
            message_count=len(trimmed_messages),
            created_at=existing.metadata.created_at if existing is not None else now,
            updated_at=now,
        )
        payload = self._record_to_payload(HistoryRecord(messages=trimmed_messages, metadata=metadata))
        if self.redis is not None:
            await self.redis.set(self._build_key(scope), payload, ex=self.ttl_seconds)
            return
        self._memory_store[self._build_key(scope)] = payload

    async def delete_messages(
        self,
        session_id: str | None,
        *,
        request_context: RequestContext | None = None,
        agent_id: str | None = None,
    ) -> None:
        """删除某个会话的历史记录。"""

        scope = self._build_scope(session_id, request_context=request_context, agent_id=agent_id)
        if scope is None:
            return

        if self.redis is not None:
            await self.redis.delete(self._build_key(scope))
            return
        self._memory_store.pop(self._build_key(scope), None)

    async def _get_payload(self, scope: HistoryScope) -> str | bytes | None:
        key = self._build_key(scope)
        if self.redis is not None:
            return await self.redis.get(key)
        return self._memory_store.get(key)

    def _build_key(self, scope: HistoryScope) -> str:
        scope_hash = hashlib.sha256(
            json.dumps(asdict(scope), ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()[:24]
        return f"{self.key_prefix}:history:{scope_hash}"

    def _build_scope(
        self,
        session_id: str | None,
        *,
        request_context: RequestContext | None,
        agent_id: str | None,
    ) -> HistoryScope | None:
        if not session_id:
            return None
        return HistoryScope(
            session_id=session_id,
            agent_id=agent_id,
            user_id=request_context.user_id if request_context is not None else None,
            tenant_id=request_context.tenant_id if request_context is not None else None,
        )

    def _trim_messages(self, messages: list[ModelMessage]) -> list[ModelMessage]:
        if self.max_messages is None or self.max_messages <= 0:
            return messages
        return messages[-self.max_messages :]

    def _record_to_payload(self, record: HistoryRecord) -> str:
        metadata = asdict(record.metadata)
        for key in ("created_at", "updated_at"):
            value = metadata.get(key)
            if isinstance(value, datetime):
                metadata[key] = value.isoformat()
        return json.dumps(
            {
                "version": 1,
                "metadata": metadata,
                "messages": json.loads(ModelMessagesTypeAdapter.dump_json(record.messages).decode()),
            },
            ensure_ascii=False,
        )

    def _record_from_payload(self, payload: str | bytes, scope: HistoryScope) -> HistoryRecord:
        if isinstance(payload, bytes):
            payload = payload.decode()
        data = json.loads(payload)
        if isinstance(data, list):
            messages = ModelMessagesTypeAdapter.validate_json(payload)
            metadata = HistoryMetadata(
                session_id=scope.session_id,
                agent_id=scope.agent_id,
                user_id=scope.user_id,
                tenant_id=scope.tenant_id,
                message_count=len(messages),
            )
            return HistoryRecord(messages=messages, metadata=metadata)

        messages = ModelMessagesTypeAdapter.validate_python(data.get("messages", []))
        metadata_payload = dict(data.get("metadata") or {})
        for key in ("created_at", "updated_at"):
            value = metadata_payload.get(key)
            if isinstance(value, str):
                metadata_payload[key] = datetime.fromisoformat(value)
        metadata = HistoryMetadata(
            session_id=metadata_payload.get("session_id") or scope.session_id,
            agent_id=metadata_payload.get("agent_id", scope.agent_id),
            user_id=metadata_payload.get("user_id", scope.user_id),
            tenant_id=metadata_payload.get("tenant_id", scope.tenant_id),
            model=metadata_payload.get("model"),
            skills=list(metadata_payload.get("skills") or []),
            mcp_servers=list(metadata_payload.get("mcp_servers") or []),
            usage=metadata_payload.get("usage"),
            message_count=int(metadata_payload.get("message_count") or len(messages)),
            created_at=metadata_payload.get("created_at") or datetime.now(UTC),
            updated_at=metadata_payload.get("updated_at") or datetime.now(UTC),
        )
        return HistoryRecord(messages=messages, metadata=metadata)
