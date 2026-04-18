from __future__ import annotations

from collections.abc import Callable, Sequence

from pydantic_ai.toolsets.abstract import AbstractToolset

from app.ai.deps import AgentDeps
from app.ai.exceptions import SkillConfigurationError
from app.ai.toolsets.builtin import (
    BUILTIN_REQUEST_TOOLSET_ID,
    BUILTIN_RUNTIME_TOOLSET_ID,
    BUILTIN_TIME_TOOLSET_ID,
    get_builtin_request_toolset,
    get_builtin_runtime_toolset,
    get_builtin_time_toolset,
)

# 当前 `chat-agent` 默认已经静态挂载了全部 builtin toolsets。
# Skills 如果声明依赖这些 id，不需要再重复动态装配，避免同名工具重复注册。
STATIC_DEFAULT_TOOLSET_IDS = frozenset(
    {
        BUILTIN_TIME_TOOLSET_ID,
        BUILTIN_REQUEST_TOOLSET_ID,
        BUILTIN_RUNTIME_TOOLSET_ID,
    }
)

TOOLSET_BUILDERS: dict[str, Callable[[], AbstractToolset[AgentDeps]]] = {
    BUILTIN_TIME_TOOLSET_ID: get_builtin_time_toolset,
    BUILTIN_REQUEST_TOOLSET_ID: get_builtin_request_toolset,
    BUILTIN_RUNTIME_TOOLSET_ID: get_builtin_runtime_toolset,
}


def build_registered_toolsets(toolset_ids: Sequence[str]) -> list[AbstractToolset[AgentDeps]]:
    """按稳定 toolset id 构建动态 toolsets。

    目前首期只暴露一小组已注册的 toolset builder。
    后续如果引入业务 toolsets / skill 专属 toolsets，可以继续把映射表扩充到这里。
    """

    resolved_toolsets: list[AbstractToolset[AgentDeps]] = []
    seen: set[str] = set()

    for toolset_id in toolset_ids:
        if toolset_id in seen or toolset_id in STATIC_DEFAULT_TOOLSET_IDS:
            continue

        builder = TOOLSET_BUILDERS.get(toolset_id)
        if builder is None:
            raise SkillConfigurationError(f"Skill 依赖了未注册的 toolset: {toolset_id}")

        seen.add(toolset_id)
        resolved_toolsets.append(builder())

    return resolved_toolsets
