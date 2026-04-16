from typing import Any

from pydantic import BaseModel, Field


class AgentChatRequest(BaseModel):
    """`POST /api/v1/agents/chat` 的请求体。"""
    agent_id: str | None = Field(default=None, description="目标 Agent ID")
    message: str = Field(min_length=1, description="用户输入")
    session_id: str | None = Field(default=None, description="会话 ID")
    model: str | None = Field(default=None, description="覆盖模型名")


class AgentChatResponse(BaseModel):
    """一次 chat run 的标准化响应结构。"""
    run_id: str = Field(description="运行 ID")
    agent_id: str = Field(description="Agent ID")
    model: str = Field(description="实际使用的模型")
    message: str = Field(description="Agent 输出")
    request_id: str = Field(description="请求 ID")
    session_id: str | None = Field(default=None, description="会话 ID")
    usage: dict[str, Any] | None = Field(default=None, description="模型用量信息")
