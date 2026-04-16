from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass(slots=True)
class ToolExposureRecord:
    """记录某次 run 开始执行前，对模型可见的工具集合。"""
    agent_id: str
    request_id: str
    tool_names: list[str]
    tool_metadata: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass(slots=True)
class ToolExecutionRecord:
    """记录某次真实工具调用的最小执行事件。"""
    agent_id: str
    request_id: str
    tool_name: str
    tool_call_id: str | None
    status: Literal["success", "error"]
    tool_args: dict[str, Any] = field(default_factory=dict)
    tool_metadata: dict[str, Any] = field(default_factory=dict)
    result: Any = None
    error: str | None = None


class ToolAuditService:
    """Minimal in-memory audit service for tool exposure and execution debugging."""

    def __init__(self) -> None:
        # 当前先使用内存列表，方便本阶段调试与测试。
        # 后续如果要做持久化或可观测平台接入，再替换存储后端即可。
        self._exposure_records: list[ToolExposureRecord] = []
        self._execution_records: list[ToolExecutionRecord] = []

    def record_tool_exposure(
        self,
        *,
        agent_id: str,
        request_id: str,
        tool_names: list[str],
        tool_metadata: dict[str, dict[str, Any]],
    ) -> None:
        """追加一条 tool exposure 记录。"""
        self._exposure_records.append(
            ToolExposureRecord(
                agent_id=agent_id,
                request_id=request_id,
                tool_names=tool_names,
                tool_metadata=tool_metadata,
            )
        )

    def record_tool_execution(
        self,
        *,
        agent_id: str,
        request_id: str,
        tool_name: str,
        tool_call_id: str | None,
        status: Literal["success", "error"],
        tool_args: dict[str, Any],
        tool_metadata: dict[str, Any],
        result: Any = None,
        error: str | None = None,
    ) -> None:
        """追加一条 tool execution 记录。"""
        self._execution_records.append(
            ToolExecutionRecord(
                agent_id=agent_id,
                request_id=request_id,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                status=status,
                tool_args=dict(tool_args),
                tool_metadata=dict(tool_metadata),
                result=self._normalize_value(result),
                error=error,
            )
        )

    def latest_record(self) -> ToolExposureRecord | None:
        """取最近一条 exposure 记录，主要给测试和调试使用。"""
        if not self._exposure_records:
            return None
        return self._exposure_records[-1]

    def latest_execution_record(self) -> ToolExecutionRecord | None:
        """取最近一条 execution 记录，主要给测试和调试使用。"""
        if not self._execution_records:
            return None
        return self._execution_records[-1]

    def clear(self) -> None:
        """清空当前内存中的全部审计记录。"""
        self._exposure_records.clear()
        self._execution_records.clear()

    def _normalize_value(self, value: Any) -> Any:
        """把工具结果尽量整理成稳定、可断言、可序列化的结构。"""
        if value is None:
            return None
        if isinstance(value, str | int | float | bool):
            return value
        if isinstance(value, list):
            return [self._normalize_value(item) for item in value]
        if isinstance(value, dict):
            return {str(key): self._normalize_value(item) for key, item in value.items()}
        if hasattr(value, "model_dump"):
            return value.model_dump(mode="json")
        if hasattr(value, "__dict__"):
            return {str(key): self._normalize_value(item) for key, item in vars(value).items()}
        return repr(value)
