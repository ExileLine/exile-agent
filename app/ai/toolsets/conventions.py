import re
from typing import Any, TypeVar

from pydantic_ai.toolsets.function import FunctionToolset


AgentDepsT = TypeVar("AgentDepsT")

# 统一约束 tool 名称必须使用小写 snake_case。
# 这样可以让模型侧看到的函数名风格稳定，也方便后续审计和过滤。
TOOL_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")

# 当前项目把只读工具限制为若干稳定前缀。
# 这样一眼就能从工具名上看出它更偏“查询/检查”，而不是“执行/修改”。
READONLY_TOOL_PREFIXES = ("get_", "list_", "check_", "search_")


class ToolConventionError(ValueError):
    """工具集不符合项目本地约定时抛出的异常。"""


def create_function_toolset(
    *,
    id: str | None = None,
    metadata: dict[str, Any] | None = None,
    instructions: str | list[str] | tuple[str, ...] | None = None,
    strict: bool = True,
    require_parameter_descriptions: bool = True,
    **kwargs: Any,
) -> FunctionToolset[AgentDepsT]:
    """创建带项目默认约束的 FunctionToolset。

    这里不直接裸用 `FunctionToolset(...)`，而是统一经过这一层封装，
    目的是把当前项目的默认规范固化下来，避免每个 toolset 文件各写各的。

    当前默认开启的约束：
    - `strict=True`：让工具 schema 更严格，尤其对 OpenAI 兼容模型更稳定
    - `require_parameter_descriptions=True`：要求参数描述完整，减少模型误用
    """
    return FunctionToolset[AgentDepsT](
        id=id,
        metadata=metadata,
        instructions=instructions,
        strict=strict,
        require_parameter_descriptions=require_parameter_descriptions,
        **kwargs,
    )


def validate_toolset_conventions(toolset: FunctionToolset[Any]) -> None:
    """对一个已构建好的 toolset 做本地规范校验。

    这一步的目标不是参与运行时逻辑，而是尽早失败。
    如果工具定义不符合团队约定，希望在启动期或测试期就报错，
    而不是等到模型真正调用工具时才发现命名、描述或 schema 不一致。
    """
    errors: list[str] = []

    for tool_name, tool in toolset.tools.items():
        # 约束工具名风格，避免出现驼峰、混合命名或不可读缩写。
        if not TOOL_NAME_PATTERN.fullmatch(tool_name):
            errors.append(
                f"tool {tool_name!r} must use lowercase snake_case and start with a letter"
            )

        # description 本质上是给模型看的“工具说明”，不能为空，
        # 也尽量要求写成完整句子，保持整体风格稳定。
        description = (tool.description or "").strip()
        if not description:
            errors.append(f"tool {tool_name!r} must provide a non-empty description")
        elif not description.endswith("."):
            errors.append(f"tool {tool_name!r} description must end with a period")

        # strict / require_parameter_descriptions 是当前项目定义的默认 schema 规范。
        if tool.strict is not True:
            errors.append(f"tool {tool_name!r} must enable strict JSON schema mode")

        if tool.require_parameter_descriptions is not True:
            errors.append(f"tool {tool_name!r} must require parameter descriptions")

        metadata = tool.metadata or {}
        # 如果 metadata 已明确标记只读，则工具名也必须体现“只读语义”，
        # 这样命名、metadata、审计标签三者是一致的。
        if metadata.get("readonly") is True and not tool_name.startswith(READONLY_TOOL_PREFIXES):
            errors.append(
                f"readonly tool {tool_name!r} must start with one of {READONLY_TOOL_PREFIXES}"
            )

    if errors:
        raise ToolConventionError("; ".join(errors))
