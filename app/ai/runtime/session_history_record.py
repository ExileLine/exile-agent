from __future__ import annotations

from sqlalchemy import Index, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import CustomBaseModel


class AISessionHistoryRecord(CustomBaseModel):
    """每次 Agent 对话写入一条记录，供用户历史列表分页查询。"""

    __table_name__ = "ai_session_history_record"
    __table_args__ = (
        Index("ix_ai_session_history_record_user_update", "user_id", "tenant_id", "update_timestamp"),
        Index("ix_ai_session_history_record_session_user", "session_id", "user_id", "tenant_id"),
    )

    session_id: Mapped[str] = mapped_column(String(128), nullable=False, comment="会话 ID")
    user_id: Mapped[str] = mapped_column(String(128), nullable=False, comment="用户 ID")
    tenant_id: Mapped[str | None] = mapped_column(String(128), nullable=True, comment="租户 ID")
    agent_id: Mapped[str] = mapped_column(String(64), nullable=False, comment="Agent ID")
    model: Mapped[str | None] = mapped_column(String(128), nullable=True, comment="本轮模型")
    title: Mapped[str | None] = mapped_column(String(512), nullable=True, comment="对话标题")
    user_message: Mapped[str | None] = mapped_column(Text, nullable=True, comment="本轮用户消息")
    assistant_message: Mapped[str | None] = mapped_column(Text, nullable=True, comment="本轮 Assistant 消息")
    message_timestamp: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="本轮消息时间戳")
    message_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="本轮消息数")
    messages_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list, comment="本轮 PydanticAI 消息")
    metadata_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict, comment="扩展元数据")
