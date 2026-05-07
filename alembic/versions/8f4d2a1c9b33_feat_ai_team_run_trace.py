"""feat: ai team run trace

Revision ID: 8f4d2a1c9b33
Revises: d405327c6fcd
Create Date: 2026-05-07 11:20:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "8f4d2a1c9b33"
down_revision: Union[str, Sequence[str], None] = "d405327c6fcd"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ai_team_run_trace",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False, comment="ID"),
        sa.Column("team_run_id", sa.String(length=64), nullable=False, comment="Team Run ID"),
        sa.Column("run_kind", sa.String(length=32), nullable=False, comment="运行类型：chat/stream"),
        sa.Column("agent_id", sa.String(length=64), nullable=False, comment="Team Agent ID"),
        sa.Column("request_id", sa.String(length=64), nullable=False, comment="请求 ID"),
        sa.Column("session_id", sa.String(length=128), nullable=True, comment="会话 ID"),
        sa.Column("user_id", sa.String(length=128), nullable=True, comment="用户 ID"),
        sa.Column("tenant_id", sa.String(length=128), nullable=True, comment="租户 ID"),
        sa.Column("status_text", sa.String(length=32), nullable=False, comment="运行状态"),
        sa.Column("model", sa.String(length=128), nullable=False, comment="聚合 Agent 实际模型"),
        sa.Column("aggregator_agent_id", sa.String(length=64), nullable=False, comment="聚合 Agent ID"),
        sa.Column("worker_agent_ids_json", sa.JSON(), nullable=False, comment="Worker Agent ID 列表"),
        sa.Column("route_json", sa.JSON(), nullable=False, comment="AgentRouter 命中信息"),
        sa.Column("usage_json", sa.JSON(), nullable=True, comment="聚合 Agent usage"),
        sa.Column("final_message", sa.Text(), nullable=True, comment="最终聚合输出"),
        sa.Column("duration_ms", sa.Float(), nullable=True, comment="总耗时毫秒"),
        sa.Column("error", sa.Text(), nullable=True, comment="失败原因"),
        sa.Column("metadata_json", sa.JSON(), nullable=False, comment="扩展元数据"),
        sa.Column("create_time", sa.DateTime(timezone=True), nullable=False, comment="创建时间"),
        sa.Column("update_time", sa.DateTime(timezone=True), nullable=False, comment="更新时间"),
        sa.Column("create_timestamp", sa.BigInteger(), nullable=False, comment="创建时间戳"),
        sa.Column("update_timestamp", sa.BigInteger(), nullable=False, comment="更新时间戳"),
        sa.Column("is_deleted", sa.BigInteger(), nullable=True, comment="逻辑删除标识"),
        sa.Column("status", sa.BigInteger(), nullable=True, comment="状态(通用字段)"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ai_team_run_trace_team_run_id", "ai_team_run_trace", ["team_run_id"], unique=True)
    op.create_index(
        "ix_ai_team_run_trace_session_user",
        "ai_team_run_trace",
        ["session_id", "user_id", "tenant_id"],
        unique=False,
    )

    op.create_table(
        "ai_team_worker_run_trace",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False, comment="ID"),
        sa.Column("team_run_id", sa.String(length=64), nullable=False, comment="所属 Team Run ID"),
        sa.Column("worker_agent_id", sa.String(length=64), nullable=False, comment="Worker Agent ID"),
        sa.Column("role", sa.String(length=64), nullable=False, comment="Worker 角色"),
        sa.Column("status_text", sa.String(length=32), nullable=False, comment="运行状态"),
        sa.Column("model", sa.String(length=128), nullable=False, comment="Worker 实际模型"),
        sa.Column("message", sa.Text(), nullable=True, comment="Worker 输出"),
        sa.Column("error", sa.Text(), nullable=True, comment="错误信息"),
        sa.Column("error_type", sa.String(length=128), nullable=True, comment="错误类型"),
        sa.Column("duration_ms", sa.Float(), nullable=True, comment="耗时毫秒"),
        sa.Column("context_injected", sa.Boolean(), nullable=False, comment="是否注入团队上下文"),
        sa.Column("usage_json", sa.JSON(), nullable=True, comment="Worker usage"),
        sa.Column("mcp_servers_json", sa.JSON(), nullable=False, comment="实际装配的 MCP server"),
        sa.Column("skills_json", sa.JSON(), nullable=False, comment="实际装配的 Skills"),
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
        "ix_ai_team_worker_run_trace_team_run",
        "ai_team_worker_run_trace",
        ["team_run_id"],
        unique=False,
    )
    op.create_index(
        "ix_ai_team_worker_run_trace_worker",
        "ai_team_worker_run_trace",
        ["worker_agent_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_ai_team_worker_run_trace_worker", table_name="ai_team_worker_run_trace")
    op.drop_index("ix_ai_team_worker_run_trace_team_run", table_name="ai_team_worker_run_trace")
    op.drop_table("ai_team_worker_run_trace")
    op.drop_index("ix_ai_team_run_trace_session_user", table_name="ai_team_run_trace")
    op.drop_index("ix_ai_team_run_trace_team_run_id", table_name="ai_team_run_trace")
    op.drop_table("ai_team_run_trace")
