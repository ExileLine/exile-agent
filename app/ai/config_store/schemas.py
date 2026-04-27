from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.pagination import CommonPage


# Schema 分成 Create/Update/Read 三类：
# - Create/Update 接收明文 secret，由 service 负责加密后写入 ORM
# - Read 只返回脱敏状态，例如 has_api_key/header_keys/env_keys
# - ORM 中的 *_encrypted 字段不会出现在任何 Read schema 中
class AIConfigListQuery(CommonPage):
    keyword: str | None = Field(default=None, description="模糊搜索关键词")


class ModelProviderCreate(BaseModel):
    provider_key: str = Field(min_length=1, max_length=64, description="供应商内部稳定标识，例如 openai、deepseek")
    name: str = Field(min_length=1, max_length=128, description="供应商展示名称")
    provider_type: str = Field(min_length=1, max_length=64, description="供应商类型，例如 openai_compatible、anthropic、local")
    base_url: str | None = Field(default=None, max_length=512, description="供应商 API 基础地址，OpenAI-compatible 服务通常需要配置")
    api_key: str | None = Field(default=None, min_length=1, description="供应商 API Key，只写入，不会在读取接口返回")
    enabled: bool = Field(default=True, description="是否允许运行时使用该供应商")
    timeout_seconds: float | None = Field(default=None, description="供应商请求默认超时时间，单位秒")
    max_retries: int | None = Field(default=None, description="供应商请求默认最大重试次数")
    metadata_json: dict[str, Any] = Field(default_factory=dict, description="供应商扩展元数据")


class ModelProviderUpdate(BaseModel):
    # Pydantic 会通过 model_fields_set 区分“未传 api_key”和“显式传 api_key”。
    # service 依赖这个差异决定是否覆盖数据库里的 encrypted secret。
    name: str | None = Field(default=None, min_length=1, max_length=128, description="供应商展示名称")
    provider_type: str | None = Field(default=None, min_length=1, max_length=64, description="供应商类型")
    base_url: str | None = Field(default=None, max_length=512, description="供应商 API 基础地址")
    api_key: str | None = Field(default=None, min_length=1, description="新的供应商 API Key；仅显式传入时覆盖原密钥")
    enabled: bool | None = Field(default=None, description="是否允许运行时使用该供应商")
    timeout_seconds: float | None = Field(default=None, description="供应商请求默认超时时间，单位秒")
    max_retries: int | None = Field(default=None, description="供应商请求默认最大重试次数")
    metadata_json: dict[str, Any] | None = Field(default=None, description="供应商扩展元数据，显式传入时整体替换")


class ModelProviderRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int | None = Field(description="数据库主键 ID，编辑接口使用")
    provider_key: str = Field(description="供应商内部稳定标识")
    name: str = Field(description="供应商展示名称")
    provider_type: str = Field(description="供应商类型")
    base_url: str | None = Field(description="供应商 API 基础地址")
    enabled: bool = Field(description="是否允许运行时使用该供应商")
    timeout_seconds: float | None = Field(description="供应商请求默认超时时间，单位秒")
    max_retries: int | None = Field(description="供应商请求默认最大重试次数")
    metadata_json: dict[str, Any] = Field(description="供应商扩展元数据")
    has_api_key: bool = Field(description="是否已配置 API Key；不会返回明文或密文")


class AIModelCreate(BaseModel):
    model_key: str = Field(min_length=1, max_length=64, description="模型内部稳定标识，供 Agent 配置和请求覆盖使用")
    provider_key: str = Field(min_length=1, max_length=64, description="所属模型供应商 key")
    model_name: str = Field(min_length=1, max_length=128, description="供应商侧真实模型名")
    display_name: str | None = Field(default=None, max_length=128, description="模型展示名称")
    enabled: bool = Field(default=True, description="是否允许运行时使用该模型")
    context_window: int | None = Field(default=None, description="模型上下文窗口 token 数")
    max_output_tokens: int | None = Field(default=None, description="该模型允许的最大输出 token 数")
    supports_stream: bool = Field(default=True, description="是否支持流式输出")
    supports_tools: bool = Field(default=True, description="是否支持工具调用")
    supports_json_output: bool = Field(default=False, description="是否支持 JSON/结构化输出")
    input_price_per_1k: float | None = Field(default=None, description="输入 token 单价，按每 1000 token 计")
    output_price_per_1k: float | None = Field(default=None, description="输出 token 单价，按每 1000 token 计")
    risk_level: Literal["low", "medium", "high"] = Field(default="low", description="模型风险等级，用于策略和审批")
    metadata_json: dict[str, Any] = Field(default_factory=dict, description="模型扩展元数据")


class AIModelUpdate(BaseModel):
    provider_key: str | None = Field(default=None, min_length=1, max_length=64, description="所属模型供应商 key")
    model_name: str | None = Field(default=None, min_length=1, max_length=128, description="供应商侧真实模型名")
    display_name: str | None = Field(default=None, max_length=128, description="模型展示名称")
    enabled: bool | None = Field(default=None, description="是否允许运行时使用该模型")
    context_window: int | None = Field(default=None, description="模型上下文窗口 token 数")
    max_output_tokens: int | None = Field(default=None, description="该模型允许的最大输出 token 数")
    supports_stream: bool | None = Field(default=None, description="是否支持流式输出")
    supports_tools: bool | None = Field(default=None, description="是否支持工具调用")
    supports_json_output: bool | None = Field(default=None, description="是否支持 JSON/结构化输出")
    input_price_per_1k: float | None = Field(default=None, description="输入 token 单价，按每 1000 token 计")
    output_price_per_1k: float | None = Field(default=None, description="输出 token 单价，按每 1000 token 计")
    risk_level: Literal["low", "medium", "high"] | None = Field(default=None, description="模型风险等级")
    metadata_json: dict[str, Any] | None = Field(default=None, description="模型扩展元数据，显式传入时整体替换")


class AIModelRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int | None = Field(description="数据库主键 ID，编辑接口使用")
    model_key: str = Field(description="模型内部稳定标识")
    provider_key: str = Field(description="所属模型供应商 key")
    model_name: str = Field(description="供应商侧真实模型名")
    display_name: str | None = Field(description="模型展示名称")
    enabled: bool = Field(description="是否允许运行时使用该模型")
    context_window: int | None = Field(description="模型上下文窗口 token 数")
    max_output_tokens: int | None = Field(description="该模型允许的最大输出 token 数")
    supports_stream: bool = Field(description="是否支持流式输出")
    supports_tools: bool = Field(description="是否支持工具调用")
    supports_json_output: bool = Field(description="是否支持 JSON/结构化输出")
    input_price_per_1k: float | None = Field(description="输入 token 单价，按每 1000 token 计")
    output_price_per_1k: float | None = Field(description="输出 token 单价，按每 1000 token 计")
    risk_level: str = Field(description="模型风险等级")
    metadata_json: dict[str, Any] = Field(description="模型扩展元数据")


class AgentConfigCreate(BaseModel):
    agent_id: str = Field(min_length=1, max_length=64, description="Agent ID，对应运行时注册表中的 agent_id")
    enabled: bool = Field(default=True, description="是否启用该 Agent 配置")
    default_model_key: str | None = Field(default=None, max_length=64, description="Agent 默认模型 key")
    allowed_model_keys_json: list[str] = Field(default_factory=list, description="该 Agent 允许使用的模型 key 列表")
    default_skill_ids_json: list[str] = Field(default_factory=list, description="该 Agent 默认启用的 skill ID 列表")
    default_mcp_server_ids_json: list[str] = Field(default_factory=list, description="该 Agent 默认启用的 MCP server key 列表")
    allow_request_model_override: bool = Field(default=False, description="是否允许请求显式覆盖模型")
    allow_request_mcp_override: bool = Field(default=False, description="是否允许请求显式覆盖 MCP server")
    supports_stream: bool = Field(default=True, description="该 Agent 是否支持流式响应")
    approval_policy_key: str | None = Field(default=None, max_length=64, description="该 Agent 使用的审批策略 key")
    metadata_json: dict[str, Any] = Field(default_factory=dict, description="Agent 配置扩展元数据")


class AgentConfigUpdate(BaseModel):
    enabled: bool | None = Field(default=None, description="是否启用该 Agent 配置")
    default_model_key: str | None = Field(default=None, max_length=64, description="Agent 默认模型 key")
    allowed_model_keys_json: list[str] | None = Field(default=None, description="该 Agent 允许使用的模型 key 列表，显式传入时整体替换")
    default_skill_ids_json: list[str] | None = Field(default=None, description="该 Agent 默认启用的 skill ID 列表，显式传入时整体替换")
    default_mcp_server_ids_json: list[str] | None = Field(default=None, description="该 Agent 默认启用的 MCP server key 列表，显式传入时整体替换")
    allow_request_model_override: bool | None = Field(default=None, description="是否允许请求显式覆盖模型")
    allow_request_mcp_override: bool | None = Field(default=None, description="是否允许请求显式覆盖 MCP server")
    supports_stream: bool | None = Field(default=None, description="该 Agent 是否支持流式响应")
    approval_policy_key: str | None = Field(default=None, max_length=64, description="该 Agent 使用的审批策略 key")
    metadata_json: dict[str, Any] | None = Field(default=None, description="Agent 配置扩展元数据，显式传入时整体替换")


class AgentConfigRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int | None = Field(description="数据库主键 ID，编辑接口使用")
    agent_id: str = Field(description="Agent ID")
    enabled: bool = Field(description="是否启用该 Agent 配置")
    default_model_key: str | None = Field(description="Agent 默认模型 key")
    allowed_model_keys_json: list[str] = Field(description="该 Agent 允许使用的模型 key 列表")
    default_skill_ids_json: list[str] = Field(description="该 Agent 默认启用的 skill ID 列表")
    default_mcp_server_ids_json: list[str] = Field(description="该 Agent 默认启用的 MCP server key 列表")
    allow_request_model_override: bool = Field(description="是否允许请求显式覆盖模型")
    allow_request_mcp_override: bool = Field(description="是否允许请求显式覆盖 MCP server")
    supports_stream: bool = Field(description="该 Agent 是否支持流式响应")
    approval_policy_key: str | None = Field(description="该 Agent 使用的审批策略 key")
    metadata_json: dict[str, Any] = Field(description="Agent 配置扩展元数据")


class MCPServerCreate(BaseModel):
    server_key: str = Field(min_length=1, max_length=64, description="MCP server 内部稳定标识")
    name: str = Field(min_length=1, max_length=128, description="MCP server 展示名称")
    transport: Literal["stdio", "sse", "streamable-http"] = Field(description="MCP 传输方式：stdio 子进程、SSE、或 streamable HTTP")
    command: str | None = Field(default=None, max_length=256, description="stdio transport 的启动命令")
    args_json: list[str] = Field(default_factory=list, description="stdio transport 的命令参数")
    url: str | None = Field(default=None, max_length=1024, description="SSE 或 streamable HTTP 的 MCP endpoint")
    headers: dict[str, Any] = Field(default_factory=dict, description="远程 MCP 请求头，只写入，不会在读取接口返回")
    env: dict[str, Any] = Field(default_factory=dict, description="stdio MCP 子进程环境变量，只写入，不会在读取接口返回")
    cwd: str | None = Field(default=None, max_length=512, description="stdio MCP 子进程工作目录")
    tool_prefix: str | None = Field(default=None, max_length=64, description="MCP 工具名前缀，避免跨 server 工具名冲突")
    enabled: bool = Field(default=True, description="是否允许运行时使用该 MCP server")
    auto_route_enabled: bool = Field(default=True, description="未显式指定 MCP 时是否允许按消息关键词自动路由到该 server")
    route_keywords_json: list[str] = Field(default_factory=list, description="MCP 自动路由关键词列表")
    timeout_seconds: float | None = Field(default=None, description="MCP 初始化超时时间，单位秒")
    read_timeout_seconds: float | None = Field(default=None, description="MCP 长连接读取超时时间，单位秒")
    max_retries: int | None = Field(default=None, description="MCP 工具调用最大重试次数")
    include_instructions: bool = Field(default=False, description="是否将 MCP server instructions 注入模型上下文")
    risk_level: Literal["low", "medium", "high"] = Field(default="low", description="MCP server 风险等级")
    metadata_json: dict[str, Any] = Field(default_factory=dict, description="MCP server 扩展元数据")


class MCPServerUpdate(BaseModel):
    # headers/env 与 provider api_key 一样，只要请求体显式传入，就整体替换对应 encrypted mapping。
    name: str | None = Field(default=None, min_length=1, max_length=128, description="MCP server 展示名称")
    transport: Literal["stdio", "sse", "streamable-http"] | None = Field(default=None, description="MCP 传输方式")
    command: str | None = Field(default=None, max_length=256, description="stdio transport 的启动命令")
    args_json: list[str] | None = Field(default=None, description="stdio transport 的命令参数，显式传入时整体替换")
    url: str | None = Field(default=None, max_length=1024, description="SSE 或 streamable HTTP 的 MCP endpoint")
    headers: dict[str, Any] | None = Field(default=None, description="远程 MCP 请求头；显式传入时整体替换并加密保存")
    env: dict[str, Any] | None = Field(default=None, description="stdio MCP 子进程环境变量；显式传入时整体替换并加密保存")
    cwd: str | None = Field(default=None, max_length=512, description="stdio MCP 子进程工作目录")
    tool_prefix: str | None = Field(default=None, max_length=64, description="MCP 工具名前缀")
    enabled: bool | None = Field(default=None, description="是否允许运行时使用该 MCP server")
    auto_route_enabled: bool | None = Field(default=None, description="是否允许按消息关键词自动路由到该 server")
    route_keywords_json: list[str] | None = Field(default=None, description="MCP 自动路由关键词列表，显式传入时整体替换")
    timeout_seconds: float | None = Field(default=None, description="MCP 初始化超时时间，单位秒")
    read_timeout_seconds: float | None = Field(default=None, description="MCP 长连接读取超时时间，单位秒")
    max_retries: int | None = Field(default=None, description="MCP 工具调用最大重试次数")
    include_instructions: bool | None = Field(default=None, description="是否将 MCP server instructions 注入模型上下文")
    risk_level: Literal["low", "medium", "high"] | None = Field(default=None, description="MCP server 风险等级")
    metadata_json: dict[str, Any] | None = Field(default=None, description="MCP server 扩展元数据，显式传入时整体替换")


class MCPServerRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int | None = Field(description="数据库主键 ID，编辑接口使用")
    server_key: str = Field(description="MCP server 内部稳定标识")
    name: str = Field(description="MCP server 展示名称")
    transport: str = Field(description="MCP 传输方式")
    command: str | None = Field(description="stdio transport 的启动命令")
    args_json: list[str] = Field(description="stdio transport 的命令参数")
    url: str | None = Field(description="SSE 或 streamable HTTP 的 MCP endpoint")
    header_keys: list[str] = Field(description="已配置的请求头 key 列表；不会返回 header value")
    env_keys: list[str] = Field(description="已配置的环境变量 key 列表；不会返回 env value")
    cwd: str | None = Field(description="stdio MCP 子进程工作目录")
    tool_prefix: str | None = Field(description="MCP 工具名前缀")
    enabled: bool = Field(description="是否允许运行时使用该 MCP server")
    auto_route_enabled: bool = Field(description="是否允许按消息关键词自动路由到该 server")
    route_keywords_json: list[str] = Field(description="MCP 自动路由关键词列表")
    timeout_seconds: float | None = Field(description="MCP 初始化超时时间，单位秒")
    read_timeout_seconds: float | None = Field(description="MCP 长连接读取超时时间，单位秒")
    max_retries: int | None = Field(description="MCP 工具调用最大重试次数")
    include_instructions: bool = Field(description="是否将 MCP server instructions 注入模型上下文")
    risk_level: str = Field(description="MCP server 风险等级")
    metadata_json: dict[str, Any] = Field(description="MCP server 扩展元数据")


class AgentMCPBindingItem(BaseModel):
    server_key: str = Field(min_length=1, max_length=64, description="绑定到 Agent 的 MCP server key")
    enabled: bool = Field(default=True, description="是否启用该绑定")
    required_approval: bool = Field(default=False, description="该 Agent 调用该 MCP 时是否强制进入审批")
    allow_auto_route: bool = Field(default=True, description="该 Agent 是否允许自动路由到该 MCP")


class AgentMCPBindingsReplace(BaseModel):
    bindings: list[AgentMCPBindingItem] = Field(default_factory=list, description="新的 Agent-MCP 绑定列表；PUT 时整体替换")


class AgentMCPBindingRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int | None = Field(description="数据库主键 ID")
    agent_id: str = Field(description="Agent ID")
    server_key: str = Field(description="绑定的 MCP server key")
    enabled: bool = Field(description="是否启用该绑定")
    required_approval: bool = Field(description="该 Agent 调用该 MCP 时是否强制进入审批")
    allow_auto_route: bool = Field(description="该 Agent 是否允许自动路由到该 MCP")
