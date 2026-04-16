class AIRuntimeError(Exception):
    """AI 子系统的基础异常类型。"""


class AIDisabledError(AIRuntimeError):
    """AI 能力被配置关闭时抛出。"""


class AIRuntimeNotReadyError(AIRuntimeError):
    """AI runtime 尚未初始化完成或关键资源不可用时抛出。"""


class AgentNotFoundError(AIRuntimeError):
    """请求的 agent_id 在注册表中不存在时抛出。"""
