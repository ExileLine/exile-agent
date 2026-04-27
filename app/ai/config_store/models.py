from sqlalchemy import Boolean, Float, Index, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import CustomBaseModel


# 这些 ORM 只描述“控制面配置”，不直接参与一次 Agent run。
# runtime 层后续会通过 repository/resolver 把这些记录解析成 ResolvedRunConfig。
class AIModelProvider(CustomBaseModel):
    """模型供应商配置。

    这里保存 provider 级别的连接参数；secret 字段只保存加密后的值，
    管理接口后续只能返回是否已配置，不能返回明文。
    """

    __table_name__ = "ai_model_provider"

    provider_key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True, comment="供应商稳定标识")
    name: Mapped[str] = mapped_column(String(128), nullable=False, comment="供应商名称")
    provider_type: Mapped[str] = mapped_column(String(64), nullable=False, comment="供应商类型")
    base_url: Mapped[str | None] = mapped_column(String(512), nullable=True, comment="供应商 API 地址")
    api_key_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True, comment="加密后的 API Key")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, comment="是否启用")
    timeout_seconds: Mapped[float | None] = mapped_column(Float, nullable=True, comment="请求超时时间")
    max_retries: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="最大重试次数")
    metadata_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict, comment="扩展元数据")


class AIModel(CustomBaseModel):
    """具体模型配置。

    `model_key` 是系统内部稳定标识，`model_name` 是 provider 侧真实名称。
    这样可以在不改调用方配置的情况下替换供应商模型名或做灰度映射。
    """

    __table_name__ = "ai_model"

    model_key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True, comment="模型内部稳定标识")
    provider_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True, comment="供应商稳定标识")
    model_name: Mapped[str] = mapped_column(String(128), nullable=False, comment="供应商侧模型名")
    display_name: Mapped[str | None] = mapped_column(String(128), nullable=True, comment="展示名称")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, comment="是否启用")
    context_window: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="上下文窗口")
    max_output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="最大输出 token")
    supports_stream: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, comment="是否支持流式")
    supports_tools: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, comment="是否支持工具调用")
    supports_json_output: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, comment="是否支持 JSON 输出")
    input_price_per_1k: Mapped[float | None] = mapped_column(Float, nullable=True, comment="输入 token 单价/1k")
    output_price_per_1k: Mapped[float | None] = mapped_column(Float, nullable=True, comment="输出 token 单价/1k")
    risk_level: Mapped[str] = mapped_column(String(16), nullable=False, default="low", comment="风险等级")
    metadata_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict, comment="扩展元数据")


class AIAgentConfig(CustomBaseModel):
    """Agent 运行配置。

    这张表只保存 Agent 的默认能力和请求覆盖策略。
    真正的一次请求是否允许使用某个模型/MCP，仍应由 resolver 结合用户、租户和策略判断。
    """

    __table_name__ = "ai_agent_config"

    agent_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True, comment="Agent ID")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, comment="是否启用")
    default_model_key: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="默认模型 key")
    allowed_model_keys_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list, comment="允许使用的模型 key")
    default_skill_ids_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list, comment="默认 skill ID")
    default_mcp_server_ids_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list, comment="默认 MCP server")
    allow_request_model_override: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, comment="是否允许请求覆盖模型")
    allow_request_mcp_override: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, comment="是否允许请求覆盖 MCP")
    supports_stream: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, comment="是否支持流式")
    approval_policy_key: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="审批策略 key")
    metadata_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict, comment="扩展元数据")


class AIMCPServer(CustomBaseModel):
    """MCP server 配置。

    MCP 既可能是 stdio 子进程，也可能是远程 HTTP/SSE 服务。
    这里保存的是可被 runtime 装配的 server 定义；具体是否允许某个 Agent 使用，
    由 `AIAgentMCPBinding` 和后续策略层共同决定。
    """

    __table_name__ = "ai_mcp_server"

    server_key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True, comment="MCP server 稳定标识")
    name: Mapped[str] = mapped_column(String(128), nullable=False, comment="MCP server 名称")
    transport: Mapped[str] = mapped_column(String(32), nullable=False, comment="传输方式")
    command: Mapped[str | None] = mapped_column(String(256), nullable=True, comment="stdio 启动命令")
    args_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list, comment="stdio 命令参数")
    url: Mapped[str | None] = mapped_column(String(1024), nullable=True, comment="远程 MCP 地址")
    headers_encrypted_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict, comment="加密后的请求头")
    env_encrypted_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict, comment="加密后的环境变量")
    cwd: Mapped[str | None] = mapped_column(String(512), nullable=True, comment="stdio 工作目录")
    tool_prefix: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="工具名前缀")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, comment="是否启用")
    auto_route_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, comment="是否允许自动路由")
    route_keywords_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list, comment="自动路由关键词")
    timeout_seconds: Mapped[float | None] = mapped_column(Float, nullable=True, comment="初始化超时")
    read_timeout_seconds: Mapped[float | None] = mapped_column(Float, nullable=True, comment="读取超时")
    max_retries: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="最大重试次数")
    include_instructions: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, comment="是否注入 MCP instructions")
    risk_level: Mapped[str] = mapped_column(String(16), nullable=False, default="low", comment="风险等级")
    metadata_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict, comment="扩展元数据")


class AIAgentMCPBinding(CustomBaseModel):
    """Agent 与 MCP server 的绑定关系。

    绑定关系是 MCP allowlist 的第一层。
    即使请求显式传入某个 server_key，resolver 也必须先检查这里是否允许该 Agent 使用。
    """

    __table_name__ = "ai_agent_mcp_binding"
    __table_args__ = (
        Index("ix_ai_agent_mcp_binding_agent_server", "agent_id", "server_key", unique=True),
    )

    agent_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True, comment="Agent ID")
    server_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True, comment="MCP server 稳定标识")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, comment="是否启用绑定")
    required_approval: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, comment="是否强制审批")
    allow_auto_route: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, comment="是否允许自动路由")
