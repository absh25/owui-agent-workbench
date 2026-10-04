from __future__ import annotations

import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Optional

from .approval_store import arguments_hash, canonical_json
from .models import ToolExecutionContext, WorkflowDecision


_SENSITIVE_KEY = re.compile(r"password|token|secret|authorization|cookie|api[_-]?key", re.I)


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]" if _SENSITIVE_KEY.search(str(key)) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


class AuditLogger:
    def __init__(self, database_path: str | Path):
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS audit_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    user_id TEXT,
                    chat_id TEXT,
                    tool_name TEXT NOT NULL,
                    arguments_hash TEXT NOT NULL,
                    arguments_redacted_json TEXT NOT NULL,
                    policy_version TEXT,
                    workflow_version TEXT,
                    decision TEXT NOT NULL,
                    matched_rule TEXT,
                    status TEXT NOT NULL,
                    result_redacted_json TEXT,
                    error TEXT
                )
                """
            )
            connection.commit()
        finally:
            connection.close()

    def write(
        self,
        *,
        context: ToolExecutionContext,
        tool_name: str,
        arguments: dict[str, Any],
        policy_version: Optional[str],
        decision: WorkflowDecision,
        status: str,
        result: Any = None,
        error: Optional[str] = None,
    ) -> None:
        values = (
            context.timestamp.isoformat(),
            context.request_id,
            context.user_id,
            context.chat_id,
            tool_name,
            arguments_hash(arguments),
            canonical_json(redact(arguments)),
            policy_version,
            decision.workflow_version,
            decision.decision,
            decision.matched_rule,
            status,
            canonical_json(redact(result)) if result is not None else None,
            error,
        )
        connection = self._connect()
        try:
            connection.execute(
                """
                INSERT INTO audit_logs (
                    timestamp, request_id, user_id, chat_id, tool_name,
                    arguments_hash, arguments_redacted_json, policy_version,
                    workflow_version, decision, matched_rule, status,
                    result_redacted_json, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )
            connection.commit()
        finally:
            connection.close()
