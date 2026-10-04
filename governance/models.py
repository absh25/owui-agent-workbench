from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Literal, Optional
import uuid


DecisionType = Literal["PASS", "AUTO", "REVIEW", "DENY"]


@dataclass(frozen=True)
class ToolExecutionContext:
    """Trusted request identity supplied by Open WebUI, never by the model."""

    user_id: Optional[str] = None
    user_email: Optional[str] = None
    user_name: Optional[str] = None
    user_roles: tuple[str, ...] = field(default_factory=tuple)
    user_groups: tuple[str, ...] = field(default_factory=tuple)
    user_permissions: tuple[str, ...] = field(default_factory=tuple)
    chat_id: Optional[str] = None
    session_id: Optional[str] = None
    message_id: Optional[str] = None
    model_id: Optional[str] = None
    tool_name: Optional[str] = None
    request_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def for_tool(self, tool_name: str) -> "ToolExecutionContext":
        return replace(
            self,
            tool_name=tool_name,
            request_id=str(uuid.uuid4()),
            timestamp=datetime.now(timezone.utc),
        )

    def identity_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "user_email": self.user_email,
            "user_name": self.user_name,
            "user_roles": list(self.user_roles),
            "user_groups": list(self.user_groups),
            "user_permissions": list(self.user_permissions),
            "chat_id": self.chat_id,
            "session_id": self.session_id,
            "message_id": self.message_id,
            "model_id": self.model_id,
        }


@dataclass(frozen=True)
class WorkflowDecision:
    decision: DecisionType
    reason: Optional[str] = None
    matched_rule: Optional[str] = None
    approver_group: Optional[str] = None
    workflow_version: Optional[str] = None
