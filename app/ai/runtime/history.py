from __future__ import annotations

from collections.abc import Sequence

from pydantic_ai import ModelMessagesTypeAdapter
from pydantic_ai.messages import ModelMessage
from redis.asyncio import Redis


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
        ttl_seconds: int,
        key_prefix: str = "session",
    ) -> None:
        self.redis = redis
        self.ttl_seconds = ttl_seconds
        self.key_prefix = key_prefix
        self._memory_store: dict[str, str] = {}

    async def load_messages(self, session_id: str | None) -> list[ModelMessage]:
        """加载某个会话当前保存的完整 message history。"""

        if not session_id:
            return []

        payload = await self._get_payload(session_id)
        if not payload:
            return []
        return ModelMessagesTypeAdapter.validate_json(payload)

    async def save_messages(self, session_id: str | None, messages: Sequence[ModelMessage]) -> None:
        """覆盖写入某个会话的完整 message history。"""

        if not session_id:
            return

        payload = ModelMessagesTypeAdapter.dump_json(list(messages)).decode()
        if self.redis is not None:
            await self.redis.set(self._build_key(session_id), payload, ex=self.ttl_seconds)
            return
        self._memory_store[session_id] = payload

    async def delete_messages(self, session_id: str | None) -> None:
        """删除某个会话的历史记录。"""

        if not session_id:
            return

        if self.redis is not None:
            await self.redis.delete(self._build_key(session_id))
            return
        self._memory_store.pop(session_id, None)

    async def _get_payload(self, session_id: str) -> str | bytes | None:
        if self.redis is not None:
            return await self.redis.get(self._build_key(session_id))
        return self._memory_store.get(session_id)

    def _build_key(self, session_id: str) -> str:
        return f"{self.key_prefix}:{session_id}:messages"
