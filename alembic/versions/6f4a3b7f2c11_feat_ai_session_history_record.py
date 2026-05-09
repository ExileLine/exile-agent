"""feat: ai session history record

Revision ID: 6f4a3b7f2c11
Revises: 8f4d2a1c9b33
Create Date: 2026-05-09 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "6f4a3b7f2c11"
down_revision: Union[str, Sequence[str], None] = "8f4d2a1c9b33"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ai_session_history_record",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False, comment="ID"),
        sa.Column("session_id", sa.String(length=128), nullable=False, comment="会话 ID"),
        sa.Column("user_id", sa.String(length=128), nullable=False, comment="用户 ID"),
        sa.Column("tenant_id", sa.String(length=128), nullable=True, comment="租户 ID"),
        sa.Column("agent_id", sa.String(length=64), nullable=False, comment="Agent ID"),
        sa.Column("model", sa.String(length=128), nullable=True, comment="本轮模型"),
        sa.Column("title", sa.String(length=512), nullable=True, comment="对话标题"),
        sa.Column("user_message", sa.Text(), nullable=True, comment="本轮用户消息"),
        sa.Column("assistant_message", sa.Text(), nullable=True, comment="本轮 Assistant 消息"),
        sa.Column("message_timestamp", sa.String(length=64), nullable=True, comment="本轮消息时间戳"),
        sa.Column("message_count", sa.Integer(), nullable=False, comment="本轮消息数"),
        sa.Column("messages_json", sa.JSON(), nullable=False, comment="本轮 PydanticAI 消息"),
        sa.Column("metadata_json", sa.JSON(), nullable=False, comment="扩展元数据"),
        sa.Column("create_time", sa.DateTime(timezone=True), nullable=False, comment="创建时间"),
        sa.Column("update_time", sa.DateTime(timezone=True), nullable=False, comment="更新时间"),
        sa.Column("create_timestamp", sa.BigInteger(), nullable=False, comment="创建时间戳"),
        sa.Column("update_timestamp", sa.BigInteger(), nullable=False, comment="更新时间戳"),
        sa.Column("is_deleted", sa.BigInteger(), nullable=True, comment="逻辑删除标识"),
        sa.Column("status", sa.BigInteger(), nullable=True, comment="状态(通用字段)"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_ai_session_history_record_user_update",
        "ai_session_history_record",
        ["user_id", "tenant_id", "update_timestamp"],
        unique=False,
    )
    op.create_index(
        "ix_ai_session_history_record_session_user",
        "ai_session_history_record",
        ["session_id", "user_id", "tenant_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_ai_session_history_record_session_user",
        table_name="ai_session_history_record",
    )
    op.drop_index(
        "ix_ai_session_history_record_user_update",
        table_name="ai_session_history_record",
    )
    op.drop_table("ai_session_history_record")
