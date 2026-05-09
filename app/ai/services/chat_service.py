from collections.abc import AsyncIterator
import json
from dataclasses import asdict

from pydantic_ai import ModelMessagesTypeAdapter

from app.ai.deps import RequestContext
from app.ai.runtime.manager import AgentManager
from app.ai.runtime.runner import AgentRunner
from app.ai.skills import SkillRegistry
from app.ai.schemas.agent import AgentManifest
from app.ai.schemas.chat import AgentChatRequest, AgentChatResponse, AgentChatResumeRequest


class ChatService:
    """面向 endpoint 的轻量服务层。

    endpoint 不直接碰 runner / manager 的细节，而是通过 service 暴露：
    - `list_agents()`
    - `chat(...)`

    这样 Web 层和 AI 运行层之间会有一层更稳定的边界。
    """
    def __init__(
        self,
        *,
        runner: AgentRunner,
        agent_manager: AgentManager,
        skill_registry: SkillRegistry | None = None,
    ) -> None:
        self.runner = runner
        self.agent_manager = agent_manager
        self.skill_registry = skill_registry

    def list_agents(self) -> list[AgentManifest]:
        return self.agent_manager.list_agents()

    def list_skills(self) -> list[dict]:
        if self.skill_registry is None:
            return []
        return [skill.model_dump(mode="json") for skill in self.skill_registry.list_skills()]

    async def get_team_run_trace(self, team_run_id: str) -> dict | None:
        return await self.runner.get_team_run_trace(team_run_id)

    async def get_session_histories(
        self,
        *,
        request_context: RequestContext,
        session_id: str,
        agent_ids: list[str] | None = None,
        merge: bool = False,
    ) -> dict:
        resolved_agent_ids = _normalize_history_agent_ids(agent_ids)
        histories = []
        for agent_id in resolved_agent_ids:
            record = await self.runner.history_store.load_record(
                session_id,
                request_context=request_context,
                agent_id=agent_id,
            )
            histories.append(_history_record_to_payload(agent_id=agent_id, record=record))

        payload = {
            "session_id": session_id,
            "histories": histories,
        }
        if merge:
            payload["messages"] = _merge_history_messages(histories)
        return payload

    async def chat(self, *, request_context: RequestContext, payload: AgentChatRequest) -> AgentChatResponse:
        """把 endpoint 请求转换成一次标准的 runner chat 调用。"""
        return await self.runner.run_chat(
            request_context=request_context,
            message=payload.message,
            agent_id=payload.agent_id,
            session_id=payload.session_id,
            model_name=payload.model,
            mcp_server_ids=payload.mcp_servers,
            skill_ids=payload.skill_ids,
            skill_tags=payload.skill_tags,
        )

    async def stream(self, *, request_context: RequestContext, payload: AgentChatRequest) -> AsyncIterator[str]:
        """把 endpoint 请求转换成一次标准的 runner stream 调用。"""

        async for event in self.runner.run_chat_stream(
            request_context=request_context,
            message=payload.message,
            agent_id=payload.agent_id,
            session_id=payload.session_id,
            model_name=payload.model,
            mcp_server_ids=payload.mcp_servers,
            skill_ids=payload.skill_ids,
            skill_tags=payload.skill_tags,
        ):
            yield event

    async def resume(self, *, request_context: RequestContext, payload: AgentChatResumeRequest) -> AgentChatResponse:
        """继续执行上一轮因 approval 停住的 run。"""

        return await self.runner.resume_chat(
            request_context=request_context,
            message_history_json=payload.message_history_json,
            approvals=payload.approvals,
            approval_id=payload.approval_id,
            agent_id=payload.agent_id,
            session_id=payload.session_id,
            model_name=payload.model,
            mcp_server_ids=payload.mcp_servers,
            skill_ids=payload.skill_ids,
            skill_tags=payload.skill_tags,
        )


def _normalize_history_agent_ids(agent_ids: list[str] | None) -> list[str]:
    source = agent_ids or ["chat-agent", "team:auto-parallel"]
    normalized: list[str] = []
    seen: set[str] = set()
    for agent_id in source:
        item = agent_id.strip()
        if not item or item in seen:
            continue
        seen.add(item)
        normalized.append(item)
    return normalized


def _history_record_to_payload(*, agent_id: str, record) -> dict:
    if record is None:
        return {
            "agent_id": agent_id,
            "exists": False,
            "metadata": None,
            "messages": [],
        }
    metadata = asdict(record.metadata)
    for key in ("created_at", "updated_at"):
        value = metadata.get(key)
        if value is not None and hasattr(value, "isoformat"):
            metadata[key] = value.isoformat()
    return {
        "agent_id": agent_id,
        "exists": True,
        "metadata": metadata,
        "messages": _sort_history_messages(
            json.loads(ModelMessagesTypeAdapter.dump_json(record.messages).decode()),
            metadata=metadata,
            history_index=0,
        ),
    }


def _merge_history_messages(histories: list[dict]) -> list[dict]:
    messages: list[tuple[tuple[str, int, int], dict]] = []
    for history_index, history in enumerate(histories):
        agent_id = history["agent_id"]
        metadata = history.get("metadata") or {}
        scoped_messages = _sort_history_messages(history["messages"], metadata=metadata, history_index=history_index)
        for message_index, message in enumerate(scoped_messages):
            item = dict(message)
            item["agent_id"] = agent_id
            messages.append(
                (
                    _history_message_sort_key(
                        item,
                        metadata=metadata,
                        history_index=history_index,
                        message_index=message_index,
                    ),
                    item,
                )
            )
    return [message for _, message in sorted(messages, key=lambda item: item[0])]


def _sort_history_messages(messages: list[dict], *, metadata: dict, history_index: int) -> list[dict]:
    return [
        message
        for _, message in sorted(
            (
                (
                    _history_message_sort_key(
                        message,
                        metadata=metadata,
                        history_index=history_index,
                        message_index=message_index,
                    ),
                    message,
                )
                for message_index, message in enumerate(messages)
            ),
            key=lambda item: item[0],
        )
    ]


def _history_message_sort_key(
    message: dict,
    *,
    metadata: dict | None = None,
    history_index: int = 0,
    message_index: int = 0,
) -> tuple[str, int, int]:
    metadata = metadata or {}
    timestamp = _history_message_timestamp(message)
    if timestamp:
        return (timestamp, history_index, message_index)
    fallback_timestamp = metadata.get("created_at") or metadata.get("updated_at") or ""
    return (fallback_timestamp, history_index, message_index)


def _history_message_timestamp(message: dict) -> str | None:
    timestamp = message.get("timestamp")
    if isinstance(timestamp, str):
        return timestamp
    parts = message.get("parts")
    if isinstance(parts, list):
        for part in parts:
            part_timestamp = part.get("timestamp") if isinstance(part, dict) else None
            if isinstance(part_timestamp, str):
                return part_timestamp
    return None
