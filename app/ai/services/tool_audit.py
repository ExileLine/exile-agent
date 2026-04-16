from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class ToolExposureRecord:
    agent_id: str
    request_id: str
    tool_names: list[str]
    tool_metadata: dict[str, dict[str, Any]] = field(default_factory=dict)


class ToolAuditService:
    """Minimal in-memory audit service for tool exposure and debugging."""

    def __init__(self) -> None:
        self._records: list[ToolExposureRecord] = []

    def record_tool_exposure(
        self,
        *,
        agent_id: str,
        request_id: str,
        tool_names: list[str],
        tool_metadata: dict[str, dict[str, Any]],
    ) -> None:
        self._records.append(
            ToolExposureRecord(
                agent_id=agent_id,
                request_id=request_id,
                tool_names=tool_names,
                tool_metadata=tool_metadata,
            )
        )

    def latest_record(self) -> ToolExposureRecord | None:
        if not self._records:
            return None
        return self._records[-1]

    def clear(self) -> None:
        self._records.clear()
