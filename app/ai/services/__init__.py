from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.ai.services.chat_service import ChatService
    from app.ai.services.tool_audit import ToolAuditService

__all__ = ["ChatService", "ToolAuditService"]


def __getattr__(name: str):
    """延迟导出 service，避免包级循环导入。"""

    if name == "ChatService":
        from app.ai.services.chat_service import ChatService

        return ChatService
    if name == "ToolAuditService":
        from app.ai.services.tool_audit import ToolAuditService

        return ToolAuditService
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
