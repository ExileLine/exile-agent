from typing import Any, Literal


# `kind` 用于标记工具集的来源类型，便于后续统一治理：
# 例如 builtin / 业务工具 / MCP / skill / wrapper。
ToolsetKind = Literal["builtin", "business", "mcp", "skill", "wrapper"]

# `risk` 是面向治理的风险分级字段，
# 当前主要用于约定结构，后续可扩展到审批、审计和策略控制。
ToolRisk = Literal["low", "medium", "high"]


def build_toolset_metadata(
    *,
    toolset_id: str,
    kind: ToolsetKind,
    owner: str,
    readonly: bool,
    risk: ToolRisk = "low",
    approval_required: bool = False,
    tags: list[str] | None = None,
) -> dict[str, Any]:
    """构造 toolset 级 metadata。

    这份 metadata 会作为“默认标签”合并到该 toolset 的每个工具上，
    适合放整个工具集共享的治理信息，例如：
    - 它属于哪个 toolset
    - 它来自哪一类能力来源
    - 默认风险级别与审批要求
    """
    return {
        "toolset": {
            "id": toolset_id,
            "kind": kind,
            "owner": owner,
        },
        "readonly": readonly,
        "risk": risk,
        "approval_required": approval_required,
        "tags": list(tags or []),
    }


def build_tool_metadata(
    *,
    category: str,
    readonly: bool,
    risk: ToolRisk = "low",
    approval_required: bool = False,
    tags: list[str] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """构造单个工具的 metadata。

    这里主要放“工具自身”的标签，例如 category、readonly、risk。
    如果某些工具还需要附加额外字段，可以通过 `extra` 继续补充。

    注意：
    - 这里返回普通 `dict[str, Any]`
    - 这样可以直接匹配 `FunctionToolset` / `Tool` 的 metadata 类型签名
    """
    metadata: dict[str, Any] = {
        "category": category,
        "readonly": readonly,
        "risk": risk,
        "approval_required": approval_required,
        "tags": list(tags or []),
    }
    metadata.update(extra)
    return metadata
