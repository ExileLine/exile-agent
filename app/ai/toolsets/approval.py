from dataclasses import dataclass
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.toolsets.abstract import AbstractToolset
from pydantic_ai.toolsets.approval_required import ApprovalRequiredToolset
from pydantic_ai.tools import ToolDefinition

from app.ai.deps import AgentDeps


def tool_requires_approval(
    ctx: RunContext[AgentDeps],
    tool_def: ToolDefinition,
    tool_args: dict[str, Any],
) -> bool:
    """根据 tool metadata 判断本次调用是否应进入 approval 流程。

    当前先采用一套最小规则：
    - metadata 显式声明 `approval_required=True`
    - 或者 metadata 风险等级为 `high`

    这样当前阶段就已经有稳定的策略入口，
    后续如果要叠加用户角色、租户、环境、参数内容判断，也继续从这里扩展。
    """
    # 当前先不看 ctx 和 args 内容，只按 metadata 做静态策略判断。
    # 这样能先把“审批能力接入点”打通，后面再逐步细化动态策略。
    del ctx, tool_args
    metadata = tool_def.metadata or {}
    if metadata.get("approval_required") is True:
        return True
    return metadata.get("risk") == "high"


@dataclass
class MetadataApprovalToolset(ApprovalRequiredToolset[AgentDeps]):
    """基于 metadata 的 approval wrapper。

    它不关心某个具体 toolset 是 builtin、business 还是 MCP，
    只要工具 metadata 满足 approval 策略，就统一拦截成 `ApprovalRequired`。
    """

    @property
    def id(self) -> str | None:
        # 给 wrapper 自己一个稳定身份，方便调试、日志和后续多层 wrapper 并存时排查来源。
        wrapped_id = self.wrapped.id or "anonymous-toolset"
        return f"approval-wrapper:{wrapped_id}"


def wrap_toolset_with_metadata_approval(
    toolset: AbstractToolset[AgentDeps],
) -> AbstractToolset[AgentDeps]:
    """给单个 toolset 包一层基于 metadata 的 approval 策略。

    当前只预留“是否需要审批”的判定能力，
    真正的审批结果回填与 resume 协议，仍放在后续阶段继续实现。
    """
    return MetadataApprovalToolset(
        wrapped=toolset,
        approval_required_func=tool_requires_approval,
    )


def wrap_toolsets_with_metadata_approval(
    toolsets: list[AbstractToolset[AgentDeps]],
) -> list[AbstractToolset[AgentDeps]]:
    """给一组 toolsets 统一包上 metadata approval wrapper。"""
    return [wrap_toolset_with_metadata_approval(toolset) for toolset in toolsets]
