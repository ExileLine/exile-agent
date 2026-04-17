class AIRuntimeError(Exception):
    """AI 子系统的基础异常类型。"""


class AIDisabledError(AIRuntimeError):
    """AI 能力被配置关闭时抛出。"""


class AIRuntimeNotReadyError(AIRuntimeError):
    """AI runtime 尚未初始化完成或关键资源不可用时抛出。"""


class AgentNotFoundError(AIRuntimeError):
    """请求的 agent_id 在注册表中不存在时抛出。"""


class AIRunExecutionError(AIRuntimeError):
    """一次具体 Agent run 在执行阶段失败时抛出。"""


class MCPConfigurationError(AIRuntimeError):
    """MCP 配置格式不正确或与当前运行方式不兼容时抛出。"""


class MCPServerNotFoundError(AIRuntimeError):
    """请求了未注册的 MCP server ID 时抛出。"""


class MCPRuntimeError(AIRuntimeError):
    """MCP server 在初始化、列工具或执行过程中失败时抛出。"""
