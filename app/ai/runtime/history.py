from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from loguru import logger
from pydantic_ai import ModelMessagesTypeAdapter
from pydantic_ai.messages import ModelMessage, UserPromptPart
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.deps import RequestContext
from app.ai.runtime.session_history_record import AISessionHistoryRecord


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
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    message_count: int = 0
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(slots=True)
class HistoryRecord:
    """持久化到 Redis/内存中的完整会话历史快照。"""

    messages: list[ModelMessage]
    metadata: HistoryMetadata


@dataclass(slots=True)
class SessionConversationRecord:
    session_id: str
    user_id: str
    tenant_id: str | None
    agent_id: str
    model: str | None
    title: str | None
    user_message: str | None
    assistant_message: str | None
    message_timestamp: str | None
    message_count: int
    created_at: datetime
    updated_at: datetime
    messages: list[dict[str, Any]]
    artifacts: list[dict[str, Any]]
    metadata: dict[str, Any]


class SessionHistoryStore:
    """会话历史存储。

    Redis/内存保存运行时完整上下文，DB 追加保存每轮对话记录。
    对外历史列表和详情接口读取 DB 记录，测试环境可退化到内存记录。
    """

    def __init__(
        self,
        *,
        redis: Redis | None,
        ttl_seconds: int | None,
        db_session_factory: async_sessionmaker[AsyncSession] | Callable[[], AsyncSession] | None = None,
        db_enabled: bool = True,
        key_prefix: str = "session",
        max_messages: int | None = 40,
    ) -> None:
        self.redis = redis
        self.ttl_seconds = ttl_seconds
        self.db_session_factory = db_session_factory
        self.db_enabled = db_enabled
        self.key_prefix = key_prefix
        self.max_messages = max_messages
        self._memory_store: dict[str, str] = {}
        self._memory_conversation_records: list[SessionConversationRecord] = []

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
        artifacts: Sequence[dict[str, Any]] = (),
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
            artifacts=[dict(item) for item in artifacts],
            message_count=len(trimmed_messages),
            created_at=existing.metadata.created_at if existing is not None else now,
            updated_at=now,
        )
        payload = self._record_to_payload(HistoryRecord(messages=trimmed_messages, metadata=metadata))
        if self.redis is not None:
            await self.redis.set(self._build_key(scope), payload, ex=self.ttl_seconds)
        else:
            self._memory_store[self._build_key(scope)] = payload
        await self._insert_conversation_record(
            previous_messages=existing.messages if existing is not None else [],
            current_messages=trimmed_messages,
            metadata=metadata,
        )

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
        else:
            self._memory_store.pop(self._build_key(scope), None)
        await self._delete_conversation_records(
            session_id=session_id,
            request_context=request_context,
            agent_id=agent_id,
        )

    async def paginate_user_sessions(
        self,
        *,
        user_id: str,
        tenant_id: str | None = None,
        agent_ids: list[str] | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        """按用户分页返回会话列表，优先走数据库按 session_id 分组分页。"""

        records = await self._load_session_summaries_from_db(
            user_id=user_id,
            tenant_id=tenant_id,
            agent_ids=agent_ids,
            page=page,
            size=size,
        )
        if records is not None:
            if records[1] == 0 and self._memory_conversation_records:
                memory_records = self._paginate_session_summaries_from_memory(
                    user_id=user_id,
                    tenant_id=tenant_id,
                    agent_ids=agent_ids,
                    page=page,
                    size=size,
                )
                if memory_records[1] > 0:
                    return memory_records
            return records
        return self._paginate_session_summaries_from_memory(
            user_id=user_id,
            tenant_id=tenant_id,
            agent_ids=agent_ids,
            page=page,
            size=size,
        )

    async def load_session_history(
        self,
        *,
        session_id: str,
        user_id: str | None = None,
        tenant_id: str | None = None,
        agent_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """从对话记录表还原某个 session_id 下的完整历史详情。"""

        payload = await self._load_session_history_from_db(
            session_id=session_id,
            user_id=user_id,
            tenant_id=tenant_id,
            agent_ids=agent_ids,
        )
        if payload is not None:
            return payload
        return self._load_session_history_from_memory(
            session_id=session_id,
            user_id=user_id,
            tenant_id=tenant_id,
            agent_ids=agent_ids,
        )

    async def paginate_user_session_ids(
        self,
        *,
        user_id: str,
        tenant_id: str | None = None,
        agent_ids: list[str] | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[str], int]:
        records, total = await self.paginate_user_sessions(
            user_id=user_id,
            tenant_id=tenant_id,
            agent_ids=agent_ids,
            page=page,
            size=size,
        )
        return [item["session_id"] for item in records], total

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
            artifacts=list(metadata_payload.get("artifacts") or []),
            message_count=int(metadata_payload.get("message_count") or len(messages)),
            created_at=metadata_payload.get("created_at") or datetime.now(UTC),
            updated_at=metadata_payload.get("updated_at") or datetime.now(UTC),
        )
        return HistoryRecord(messages=messages, metadata=metadata)

    async def _insert_conversation_record(
        self,
        *,
        previous_messages: list[ModelMessage],
        current_messages: list[ModelMessage],
        metadata: HistoryMetadata,
    ) -> None:
        record = self._build_conversation_record(
            previous_messages=previous_messages,
            current_messages=current_messages,
            metadata=metadata,
        )
        if record is None:
            return

        self._memory_conversation_records.append(record)
        if not self.db_session_factory or not self.db_enabled:
            return

        try:
            async with self.db_session_factory() as session:
                session.add(
                    AISessionHistoryRecord(
                        session_id=record.session_id,
                        user_id=record.user_id,
                        tenant_id=record.tenant_id,
                        agent_id=record.agent_id,
                        model=record.model,
                        title=record.title,
                        user_message=record.user_message,
                        assistant_message=record.assistant_message,
                        message_timestamp=record.message_timestamp,
                        message_count=record.message_count,
                        messages_json=record.messages,
                        metadata_json=record.metadata,
                    )
                )
                await session.commit()
        except Exception:
            logger.exception("保存 AI 会话对话记录失败，已保留内存副本: {}", metadata.session_id)
            return

    async def _delete_conversation_records(
        self,
        *,
        session_id: str | None,
        request_context: RequestContext | None,
        agent_id: str | None,
    ) -> None:
        if not session_id or request_context is None or not request_context.user_id:
            return

        self._memory_conversation_records = [
            record
            for record in self._memory_conversation_records
            if not (
                record.session_id == session_id
                and record.user_id == request_context.user_id
                and record.tenant_id == request_context.tenant_id
                and (agent_id is None or record.agent_id == agent_id)
            )
        ]
        if not self.db_session_factory or not self.db_enabled:
            return

        try:
            async with self.db_session_factory() as session:
                filters = [
                    AISessionHistoryRecord.session_id == session_id,
                    AISessionHistoryRecord.user_id == request_context.user_id,
                    AISessionHistoryRecord.is_deleted == 0,
                    AISessionHistoryRecord.status == 1,
                ]
                if request_context.tenant_id is None:
                    filters.append(AISessionHistoryRecord.tenant_id.is_(None))
                else:
                    filters.append(AISessionHistoryRecord.tenant_id == request_context.tenant_id)
                if agent_id is not None:
                    filters.append(AISessionHistoryRecord.agent_id == agent_id)

                rows = list((await session.execute(select(AISessionHistoryRecord).where(*filters))).scalars().all())
                for row in rows:
                    row.is_deleted = 1
                    row.touch()
                if rows:
                    await session.commit()
        except Exception:
            logger.exception("删除 AI 会话对话记录失败: {}", session_id)
            return

    async def _load_session_summaries_from_db(
        self,
        *,
        user_id: str,
        tenant_id: str | None,
        agent_ids: list[str] | None,
        page: int,
        size: int,
    ) -> tuple[list[dict[str, Any]], int] | None:
        if not self.db_session_factory or not self.db_enabled:
            return None
        try:
            async with self.db_session_factory() as session:
                filters = self._record_filters(user_id=user_id, tenant_id=tenant_id, agent_ids=agent_ids)
                grouped_sessions = (
                    select(AISessionHistoryRecord.session_id)
                    .where(*filters)
                    .group_by(AISessionHistoryRecord.session_id)
                    .subquery()
                )
                total_stmt = select(func.count()).select_from(grouped_sessions)
                total = int((await session.execute(total_stmt)).scalar_one())
                offset = (page - 1) * size
                latest_ids = (
                    select(
                        AISessionHistoryRecord.session_id.label("session_id"),
                        func.max(AISessionHistoryRecord.id).label("latest_id"),
                    )
                    .where(*filters)
                    .group_by(AISessionHistoryRecord.session_id)
                    .subquery()
                )
                stmt = (
                    select(AISessionHistoryRecord)
                    .join(latest_ids, AISessionHistoryRecord.id == latest_ids.c.latest_id)
                    .order_by(
                        AISessionHistoryRecord.update_timestamp.desc(),
                        AISessionHistoryRecord.id.desc(),
                    )
                    .offset(offset)
                    .limit(size)
                )
                latest_rows = list((await session.execute(stmt)).scalars().all())
                if not latest_rows:
                    return [], total
                session_ids = [row.session_id for row in latest_rows]
                all_rows_stmt = (
                    select(AISessionHistoryRecord)
                    .where(*filters, AISessionHistoryRecord.session_id.in_(session_ids))
                    .order_by(AISessionHistoryRecord.id.asc())
                )
                all_rows = list((await session.execute(all_rows_stmt)).scalars().all())
                records_by_session = self._group_payloads_by_session(
                    [self._conversation_model_to_payload(row) for row in all_rows]
                )
                return [
                    self._session_summary_payload(records_by_session.get(row.session_id, []))
                    for row in latest_rows
                ], total
        except Exception:
            logger.exception("查询 AI 用户会话列表失败，尝试读取内存副本: {}", user_id)
            return None

    async def _load_session_history_from_db(
        self,
        *,
        session_id: str,
        user_id: str | None,
        tenant_id: str | None,
        agent_ids: list[str] | None,
    ) -> dict[str, Any] | None:
        if not self.db_session_factory or not self.db_enabled:
            return None
        try:
            async with self.db_session_factory() as session:
                filters = self._record_filters(
                    session_id=session_id,
                    user_id=user_id,
                    tenant_id=tenant_id,
                    agent_ids=agent_ids,
                )
                stmt = (
                    select(AISessionHistoryRecord)
                    .where(*filters)
                    .order_by(AISessionHistoryRecord.id.asc())
                )
                rows = list((await session.execute(stmt)).scalars().all())
                return self._session_detail_payload(
                    [self._conversation_model_to_payload(row) for row in rows],
                    session_id=session_id,
                    user_id=user_id,
                    tenant_id=tenant_id,
                )
        except Exception:
            logger.exception("查询 AI 会话详情失败，尝试读取内存副本: {}", session_id)
            return None

    def _paginate_session_summaries_from_memory(
        self,
        *,
        user_id: str,
        tenant_id: str | None,
        agent_ids: list[str] | None,
        page: int,
        size: int,
    ) -> tuple[list[dict[str, Any]], int]:
        records = [
            record
            for record in self._memory_conversation_records
            if record.user_id == user_id
            and record.tenant_id == tenant_id
            and (not agent_ids or record.agent_id in agent_ids)
        ]
        records_by_session = self._group_payloads_by_session(
            [self._conversation_record_to_payload(record) for record in records]
        )
        summaries = [
            self._session_summary_payload(session_records)
            for session_records in records_by_session.values()
        ]
        summaries.sort(key=lambda item: (item.get("updated_at") or "", item.get("session_id") or ""), reverse=True)
        total = len(summaries)
        offset = (page - 1) * size
        return summaries[offset : offset + size], total

    def _load_session_history_from_memory(
        self,
        *,
        session_id: str,
        user_id: str | None,
        tenant_id: str | None,
        agent_ids: list[str] | None,
    ) -> dict[str, Any]:
        records = [
            self._conversation_record_to_payload(record)
            for record in self._memory_conversation_records
            if record.session_id == session_id
            and (user_id is None or record.user_id == user_id)
            and record.tenant_id == tenant_id
            and (not agent_ids or record.agent_id in agent_ids)
        ]
        records.sort(key=lambda item: (item.get("created_at") or "", item.get("id") or 0))
        return self._session_detail_payload(
            records,
            session_id=session_id,
            user_id=user_id,
            tenant_id=tenant_id,
        )

    @staticmethod
    def _record_filters(
        *,
        session_id: str | None = None,
        user_id: str | None = None,
        tenant_id: str | None = None,
        agent_ids: list[str] | None = None,
    ) -> list[Any]:
        filters: list[Any] = [
            AISessionHistoryRecord.is_deleted == 0,
            AISessionHistoryRecord.status == 1,
        ]
        if session_id is not None:
            filters.append(AISessionHistoryRecord.session_id == session_id)
        if user_id is not None:
            filters.append(AISessionHistoryRecord.user_id == user_id)
        if tenant_id is None:
            filters.append(AISessionHistoryRecord.tenant_id.is_(None))
        else:
            filters.append(AISessionHistoryRecord.tenant_id == tenant_id)
        if agent_ids:
            filters.append(AISessionHistoryRecord.agent_id.in_(agent_ids))
        return filters

    def _build_conversation_record(
        self,
        *,
        previous_messages: list[ModelMessage],
        current_messages: list[ModelMessage],
        metadata: HistoryMetadata,
    ) -> SessionConversationRecord | None:
        if not metadata.session_id or not metadata.user_id or not metadata.agent_id:
            return None

        new_messages = self._extract_new_messages(previous_messages=previous_messages, current_messages=current_messages)
        if not new_messages:
            return None

        user_message = self._extract_latest_user_message(new_messages) or self._extract_latest_user_message(current_messages)
        assistant_message = self._extract_latest_assistant_message(new_messages) or self._extract_latest_assistant_message(
            current_messages
        )
        timestamp = self._message_timestamp(new_messages[-1])
        serialized_messages = json.loads(ModelMessagesTypeAdapter.dump_json(new_messages).decode())
        return SessionConversationRecord(
            session_id=metadata.session_id,
            user_id=metadata.user_id,
            tenant_id=metadata.tenant_id,
            agent_id=metadata.agent_id,
            model=metadata.model,
            title=(user_message or assistant_message or metadata.session_id)[:120],
            user_message=user_message,
            assistant_message=assistant_message,
            message_timestamp=timestamp,
            message_count=len(new_messages),
            created_at=metadata.updated_at,
            updated_at=metadata.updated_at,
            messages=serialized_messages,
            artifacts=list(metadata.artifacts),
            metadata={
                "skills": metadata.skills,
                "mcp_servers": metadata.mcp_servers,
                "usage": metadata.usage,
                "artifacts": metadata.artifacts,
            },
        )

    @staticmethod
    def _extract_new_messages(
        *,
        previous_messages: list[ModelMessage],
        current_messages: list[ModelMessage],
    ) -> list[ModelMessage]:
        if not previous_messages:
            return current_messages
        if len(current_messages) > len(previous_messages):
            return current_messages[len(previous_messages) :]
        return current_messages[-2:] if len(current_messages) >= 2 else list(current_messages)

    @staticmethod
    def _message_timestamp(message: ModelMessage) -> str:
        timestamp = getattr(message, "timestamp", None)
        if timestamp is not None and hasattr(timestamp, "isoformat"):
            return timestamp.isoformat()
        if isinstance(timestamp, str):
            return timestamp
        parts = getattr(message, "parts", None)
        if isinstance(parts, list):
            for part in parts:
                part_timestamp = getattr(part, "timestamp", None)
                if part_timestamp is not None and hasattr(part_timestamp, "isoformat"):
                    return part_timestamp.isoformat()
                if isinstance(part_timestamp, str):
                    return part_timestamp
        return ""

    @staticmethod
    def _extract_latest_user_message(messages: list[ModelMessage]) -> str | None:
        for message in reversed(messages):
            if getattr(message, "kind", None) != "request":
                continue
            text = SessionHistoryStore._message_text(message, user_only=True)
            if text:
                return text[:500]
        return None

    @staticmethod
    def _extract_latest_assistant_message(messages: list[ModelMessage]) -> str | None:
        for message in reversed(messages):
            if getattr(message, "kind", None) != "response":
                continue
            text = SessionHistoryStore._message_text(message)
            if text:
                return text[:500]
        return None

    @staticmethod
    def _message_text(message: ModelMessage, *, user_only: bool = False) -> str | None:
        parts = getattr(message, "parts", None)
        if not isinstance(parts, list):
            return None
        texts: list[str] = []
        for part in parts:
            if user_only and not isinstance(part, UserPromptPart):
                continue
            content = getattr(part, "content", None)
            if isinstance(content, str) and content.strip():
                texts.append(content.strip())
        return "\n".join(texts) if texts else None

    @staticmethod
    def _group_payloads_by_session(records: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for record in records:
            session_id = record.get("session_id")
            if not session_id:
                continue
            grouped.setdefault(session_id, []).append(record)
        for session_records in grouped.values():
            session_records.sort(
                key=lambda item: (
                    item.get("message_timestamp") or item.get("created_at") or "",
                    item.get("id") or 0,
                )
            )
        return grouped

    @staticmethod
    def _session_summary_payload(records: list[dict[str, Any]]) -> dict[str, Any]:
        if not records:
            return {}
        first = records[0]
        latest = max(
            records,
            key=lambda item: (
                item.get("updated_at") or item.get("message_timestamp") or "",
                item.get("id") or 0,
            ),
        )
        agent_ids = sorted({record["agent_id"] for record in records if record.get("agent_id")})
        messages = SessionHistoryStore._flatten_record_messages(records)
        artifacts = SessionHistoryStore._flatten_record_artifacts(records)
        return {
            "session_id": latest.get("session_id"),
            "user_id": latest.get("user_id"),
            "tenant_id": latest.get("tenant_id"),
            "title": first.get("user_message") or first.get("title") or latest.get("session_id"),
            "latest_agent_id": latest.get("agent_id"),
            "latest_message": latest.get("latest_message"),
            "turn_count": len(records),
            "message_count": len(messages),
            "artifact_count": len(artifacts),
            "artifacts": artifacts,
            "agent_ids": agent_ids,
            "created_at": first.get("created_at"),
            "updated_at": latest.get("updated_at"),
        }

    @staticmethod
    def _session_detail_payload(
        records: list[dict[str, Any]],
        *,
        session_id: str,
        user_id: str | None,
        tenant_id: str | None,
    ) -> dict[str, Any]:
        records = sorted(
            records,
            key=lambda item: (
                item.get("message_timestamp") or item.get("created_at") or "",
                item.get("id") or 0,
            ),
        )
        messages = SessionHistoryStore._flatten_record_messages(records)
        artifacts = SessionHistoryStore._flatten_record_artifacts(records)
        agent_ids = sorted({record["agent_id"] for record in records if record.get("agent_id")})
        first = records[0] if records else {}
        latest = records[-1] if records else {}
        return {
            "session_id": session_id,
            "user_id": user_id or latest.get("user_id"),
            "tenant_id": tenant_id,
            "title": first.get("user_message") or first.get("title") or session_id,
            "agent_ids": agent_ids,
            "turn_count": len(records),
            "message_count": len(messages),
            "artifact_count": len(artifacts),
            "artifacts": artifacts,
            "records": records,
            "messages": messages,
            "created_at": first.get("created_at"),
            "updated_at": latest.get("updated_at"),
        }

    @staticmethod
    def _flatten_record_messages(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        seen: set[str] = set()
        for record in records:
            agent_id = record.get("agent_id")
            artifacts = list(record.get("artifacts") or [])
            for message in record.get("messages") or []:
                item = dict(message)
                if agent_id is not None:
                    item["agent_id"] = agent_id
                if artifacts and item.get("kind") == "response":
                    item["artifacts"] = artifacts
                signature = json.dumps(item, ensure_ascii=False, sort_keys=True)
                if signature in seen:
                    continue
                seen.add(signature)
                messages.append(item)
        return [
            message
            for _, message in sorted(
                (
                    (
                        (
                            SessionHistoryStore._payload_message_timestamp(message),
                            index,
                        ),
                        message,
                    )
                    for index, message in enumerate(messages)
                ),
                key=lambda item: item[0],
            )
        ]

    @staticmethod
    def _flatten_record_artifacts(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        artifacts: list[dict[str, Any]] = []
        seen: set[str] = set()
        for record in records:
            for artifact in record.get("artifacts") or []:
                if not isinstance(artifact, dict):
                    continue
                signature = str(artifact.get("artifact_id") or artifact.get("download_url") or artifact.get("path"))
                if not signature or signature in seen:
                    continue
                seen.add(signature)
                artifacts.append(dict(artifact))
        return artifacts

    @staticmethod
    def _payload_message_timestamp(message: dict[str, Any]) -> str:
        timestamp = message.get("timestamp")
        if isinstance(timestamp, str):
            return timestamp
        parts = message.get("parts")
        if isinstance(parts, list):
            for part in parts:
                if isinstance(part, dict) and isinstance(part.get("timestamp"), str):
                    return part["timestamp"]
        return ""

    @staticmethod
    def _conversation_model_to_payload(row: AISessionHistoryRecord) -> dict[str, Any]:
        return {
            "id": row.id,
            "session_id": row.session_id,
            "user_id": row.user_id,
            "tenant_id": row.tenant_id,
            "agent_id": row.agent_id,
            "model": row.model,
            "title": row.title,
            "user_message": row.user_message,
            "assistant_message": row.assistant_message,
            "message_timestamp": row.message_timestamp,
            "latest_message": {
                "agent_id": row.agent_id,
                "kind": "response" if row.assistant_message else "request",
                "content": row.assistant_message or row.user_message,
                "timestamp": row.message_timestamp,
            },
            "message_count": row.message_count,
            "messages": list(row.messages_json or []),
            "artifacts": list((row.metadata_json or {}).get("artifacts") or []),
            "metadata": dict(row.metadata_json or {}),
            "created_at": row.create_time.isoformat() if row.create_time else None,
            "updated_at": row.update_time.isoformat() if row.update_time else None,
        }

    @staticmethod
    def _conversation_record_to_payload(record: SessionConversationRecord) -> dict[str, Any]:
        return {
            "session_id": record.session_id,
            "user_id": record.user_id,
            "tenant_id": record.tenant_id,
            "agent_id": record.agent_id,
            "model": record.model,
            "title": record.title,
            "user_message": record.user_message,
            "assistant_message": record.assistant_message,
            "message_timestamp": record.message_timestamp,
            "latest_message": {
                "agent_id": record.agent_id,
                "kind": "response" if record.assistant_message else "request",
                "content": record.assistant_message or record.user_message,
                "timestamp": record.message_timestamp,
            },
            "message_count": record.message_count,
            "messages": list(record.messages),
            "artifacts": list(record.artifacts),
            "metadata": dict(record.metadata),
            "created_at": record.created_at.isoformat(),
            "updated_at": record.updated_at.isoformat(),
        }
