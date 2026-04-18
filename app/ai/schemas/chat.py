from typing import Any, Literal

from pydantic import BaseModel, Field


class AgentChatRequest(BaseModel):
    """`POST /api/v1/agents/chat` 的请求体。"""

    agent_id: str | None = Field(default=None, description="目标 Agent ID")
    message: str = Field(min_length=1, description="用户输入")
    session_id: str | None = Field(default=None, description="会话 ID")
    model: str | None = Field(default=None, description="覆盖模型名")
    skill_ids: list[str] = Field(default_factory=list, description="显式启用的 skill ID 列表")
    skill_tags: list[str] = Field(default_factory=list, description="按标签筛选 skill 的条件列表")
    mcp_servers: list[str] = Field(default_factory=list, description="本轮额外启用的 MCP server ID 列表")


class AgentApprovalRequest(BaseModel):
    """单个待审批工具调用的返回结构。"""

    tool_call_id: str = Field(description="工具调用 ID")
    tool_name: str = Field(description="工具名")
    args: dict[str, Any] = Field(default_factory=dict, description="模型生成的工具参数")
    metadata: dict[str, Any] = Field(default_factory=dict, description="工具调用 metadata")


class AgentDeferredToolRequestsPayload(BaseModel):
    """一次 run 里待前端或外部系统处理的 deferred 请求。"""

    approvals: list[AgentApprovalRequest] = Field(default_factory=list, description="待人工审批的工具调用")
    calls: list[AgentApprovalRequest] = Field(default_factory=list, description="待外部执行的工具调用")
    message_history_json: str = Field(description="续跑所需的 message_history JSON")


class AgentApprovalDecision(BaseModel):
    """前端回传的单个审批决定。"""

    tool_call_id: str = Field(description="工具调用 ID")
    approved: bool = Field(description="是否批准执行")
    override_args: dict[str, Any] | None = Field(default=None, description="审批时覆写后的工具参数")
    denial_message: str | None = Field(default=None, description="拒绝执行时返回给模型的说明")


class AgentChatResumeRequest(BaseModel):
    """`POST /api/v1/agents/chat/resume` 的请求体。"""

    agent_id: str | None = Field(default=None, description="目标 Agent ID")
    session_id: str | None = Field(default=None, description="会话 ID")
    model: str | None = Field(default=None, description="覆盖模型名")
    skill_ids: list[str] = Field(default_factory=list, description="本轮继续启用的 skill ID 列表")
    skill_tags: list[str] = Field(default_factory=list, description="本轮继续按标签筛选的 skill 条件")
    mcp_servers: list[str] = Field(default_factory=list, description="本轮需要继续挂载的 MCP server ID 列表")
    message_history_json: str = Field(min_length=1, description="上一次 run 返回的完整 message_history JSON")
    approvals: list[AgentApprovalDecision] = Field(default_factory=list, description="人工审批结果列表")


class AgentRunMeta(BaseModel):
    """一次 run 的统一元信息。

    这部分不是模型输出，而是运行时自己补充的治理信息，
    用来让 `/chat`、`/chat/resume`、`/chat/stream` 三条链返回更稳定的上下文。
    """

    run_kind: Literal["chat", "resume", "stream"] | str = Field(description="本次运行的类型")
    stream_mode: Literal["native", "fallback"] | None = Field(default=None, description="流式模式")
    history_loaded: bool = Field(description="是否加载了已有会话历史")
    history_saved: bool = Field(description="是否写回了会话历史")
    message_count: int = Field(description="本轮 run 结束后的完整消息数")
    skills: list[str] = Field(default_factory=list, description="本轮实际解析并注入的 skill 列表")
    mcp_servers: list[str] = Field(default_factory=list, description="本轮实际装配的 MCP server ID 列表")


class AgentChatResponse(BaseModel):
    """一次 chat run 的标准化响应结构。"""

    run_id: str = Field(description="运行 ID")
    agent_id: str = Field(description="Agent ID")
    model: str = Field(description="实际使用的模型")
    status: Literal["completed", "approval_required"] = Field(description="当前 run 的状态")
    message: str | None = Field(default=None, description="Agent 输出")
    deferred_tool_requests: AgentDeferredToolRequestsPayload | None = Field(
        default=None,
        description="待审批或待外部执行的 deferred 工具请求",
    )
    request_id: str = Field(description="请求 ID")
    session_id: str | None = Field(default=None, description="会话 ID")
    usage: dict[str, Any] | None = Field(default=None, description="模型用量信息")
    meta: AgentRunMeta = Field(description="运行时元信息")
