from dataclasses import dataclass
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.toolsets.abstract import AbstractToolset, ToolsetTool
from pydantic_ai.toolsets.wrapper import WrapperToolset

from app.ai.deps import AgentDeps


@dataclass
class ToolAuditWrapperToolset(WrapperToolset[AgentDeps]):
    """给一个已有 toolset 套一层执行审计包装。

    这里的目标不是改写工具定义本身，而是在“真实工具执行”这一层统一插入横切逻辑。
    这样做的好处是：
    - 不污染具体工具函数
    - 所有 toolset 都可以复用同一套审计逻辑
    - 后续继续扩耗时、脱敏、异常分类也有稳定切入点
    """

    @property
    def id(self) -> str | None:
        # 给 wrapper 自己一个稳定且可读的身份标识。
        # 这样在日志、调试、错误信息里，能明确看出“这是哪个 toolset 外包了一层 audit wrapper”。
        wrapped_id = self.wrapped.id or "anonymous-toolset"
        return f"audit-wrapper:{wrapped_id}"

    async def call_tool(
        self,
        name: str,
        tool_args: dict[str, Any],
        ctx: RunContext[AgentDeps],
        tool: ToolsetTool[AgentDeps],
    ) -> Any:
        # `call_tool(...)` 是最关键的拦截点。
        # PydanticAI 在工具真正执行时，最终会走到 toolset 的这个方法，
        # 所以这里最适合统一记录 execution audit。
        tool_metadata = dict(tool.tool_def.metadata or {})
        agent_id = getattr(ctx.agent, "name", None) or "unknown-agent"
        request_id = ctx.deps.request.request_id

        try:
            # 先把真实调用继续委托给被包装的原始 toolset。
            result = await self.wrapped.call_tool(name, tool_args, ctx, tool)
        except Exception as exc:
            # 如果执行失败，也要记录一条 error 事件，然后继续把异常抛出去，
            # 保持原始执行语义不变。
            ctx.deps.tool_audit.record_tool_execution(
                agent_id=agent_id,
                request_id=request_id,
                tool_name=name,
                tool_call_id=ctx.tool_call_id,
                status="error",
                tool_args=tool_args,
                tool_metadata=tool_metadata,
                error=str(exc),
            )
            raise

        # 成功时记录 success 事件，并附上入参、metadata 和结果摘要。
        ctx.deps.tool_audit.record_tool_execution(
            agent_id=agent_id,
            request_id=request_id,
            tool_name=name,
            tool_call_id=ctx.tool_call_id,
            status="success",
            tool_args=tool_args,
            tool_metadata=tool_metadata,
            result=result,
        )
        return result


def wrap_toolset_with_audit(toolset: AbstractToolset[AgentDeps]) -> AbstractToolset[AgentDeps]:
    """给单个 toolset 包一层 audit wrapper。"""
    return ToolAuditWrapperToolset(wrapped=toolset)


def wrap_toolsets_with_audit(
    toolsets: list[AbstractToolset[AgentDeps]],
) -> list[AbstractToolset[AgentDeps]]:
    """给一组 toolsets 统一包上 audit wrapper。

    Agent 定义侧直接用这个聚合入口，会比手动一个个包装更稳定，
    也更方便后续替换或扩展 wrapper 策略。
    """
    return [wrap_toolset_with_audit(toolset) for toolset in toolsets]
