from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from typing import Any, Optional

from .models import ToolExecutionContext, WorkflowDecision


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def arguments_hash(arguments: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(arguments).encode("utf-8")).hexdigest()


def idempotency_key(
    context: ToolExecutionContext, tool_name: str, arguments: dict[str, Any]
) -> str:
    source = "\n".join(
        [context.user_id or "", context.chat_id or "", context.message_id or "",
         tool_name, canonical_json(arguments)]
    )
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


class ApprovalStore:
    def __init__(self, database_path: str | Path):
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS approval_requests (
                    request_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    user_id TEXT,
                    user_name TEXT,
                    user_email TEXT,
                    user_roles_json TEXT,
                    user_groups_json TEXT,
                    user_permissions_json TEXT,
                    chat_id TEXT,
                    session_id TEXT,
                    message_id TEXT,
                    model_id TEXT,
                    tool_name TEXT NOT NULL,
                    arguments_json TEXT NOT NULL,
                    arguments_hash TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    policy_version TEXT,
                    workflow_version TEXT,
                    matched_rule TEXT,
                    status TEXT NOT NULL,
                    approver_group TEXT,
                    reason TEXT,
                    approved_by TEXT,
                    approved_at TEXT,
                    rejected_by TEXT,
                    rejected_at TEXT,
                    execution_result TEXT,
                    executed_at TEXT
                )
                """
            )
            columns = {
                row[1] for row in connection.execute(
                    "PRAGMA table_info(approval_requests)"
                ).fetchall()
            }
            for name in ("approval_issue_key", "approval_issue_url"):
                if name not in columns:
                    connection.execute(
                        f"ALTER TABLE approval_requests ADD COLUMN {name} TEXT"
                    )
            connection.commit()
        finally:
            connection.close()

    def get_by_idempotency_key(self, key: str) -> Optional[dict[str, Any]]:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM approval_requests WHERE idempotency_key = ?", (key,)
            ).fetchone()
        finally:
            connection.close()
        return dict(row) if row else None

    def list_pending_for_groups(
        self, groups: tuple[str, ...] | list[str], *, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Return pending requests routed to administrators of these groups.

        The caller must obtain and validate group-admin membership from
        Open WebUI before displaying or acting on these rows.
        """
        normalized = tuple(dict.fromkeys(str(group).strip() for group in groups if str(group).strip()))
        if not normalized:
            return []
        targets = tuple(f"group-admins:{group}" for group in normalized)
        placeholders = ", ".join("?" for _ in targets)
        connection = self._connect()
        try:
            rows = connection.execute(
                f"SELECT * FROM approval_requests WHERE status = 'PENDING' "
                f"AND approver_group IN ({placeholders}) ORDER BY created_at ASC LIMIT ?",
                [*targets, max(1, min(int(limit), 1000))],
            ).fetchall()
        finally:
            connection.close()
        return [dict(row) for row in rows]

    def get(self, request_id: str) -> Optional[dict[str, Any]]:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM approval_requests WHERE request_id = ?", (request_id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            connection.close()

    def respond(self, request_id: str, context: ToolExecutionContext, *, confirmed: bool,
                arguments: dict[str, Any] | None = None) -> bool:
        """Atomically claim one pending write, scoped to its requester and chat."""
        if not context.user_id or not context.chat_id:
            return False
        actor_column, time_column = (
            ("approved_by", "approved_at") if confirmed else ("rejected_by", "rejected_at")
        )
        connection = self._connect()
        try:
            edited = ", arguments_json = ?, arguments_hash = ?" if confirmed and arguments is not None else ""
            edited_values = (canonical_json(arguments), arguments_hash(arguments)) if edited else ()
            cursor = connection.execute(
                f"UPDATE approval_requests SET status = ?, {actor_column} = ?, {time_column} = ?{edited} "
                "WHERE request_id = ? AND status = 'PENDING' AND user_id = ? AND chat_id = ?",
                ("EXECUTING" if confirmed else "CANCELLED", context.user_id,
                 datetime.now(timezone.utc).isoformat(), *edited_values, request_id, context.user_id, context.chat_id),
            )
            connection.commit()
            return cursor.rowcount == 1
        finally:
            connection.close()

    def finish(self, request_id: str, *, status: str, result: Any) -> None:
        connection = self._connect()
        try:
            connection.execute(
                "UPDATE approval_requests SET status = ?, execution_result = ?, executed_at = ? "
                "WHERE request_id = ? AND status = 'EXECUTING'",
                (status, canonical_json(result), datetime.now(timezone.utc).isoformat(), request_id),
            )
            connection.commit()
        finally:
            connection.close()

    def attach_approval_issue(
        self, request_id: str, issue: dict[str, Any]
    ) -> Optional[dict[str, Any]]:
        """Persist the Jira issue created to represent this approval request."""
        connection = self._connect()
        try:
            connection.execute(
                "UPDATE approval_requests SET approval_issue_key = ?, approval_issue_url = ? "
                "WHERE request_id = ?",
                (issue.get("key"), issue.get("url"), request_id),
            )
            connection.commit()
            row = connection.execute(
                "SELECT * FROM approval_requests WHERE request_id = ?", (request_id,)
            ).fetchone()
        finally:
            connection.close()
        return dict(row) if row else None

    def create_pending(
        self,
        *,
        context: ToolExecutionContext,
        tool_name: str,
        arguments: dict[str, Any],
        decision: WorkflowDecision,
        policy_version: str,
    ) -> tuple[dict[str, Any], bool]:
        key = idempotency_key(context, tool_name, arguments)
        existing = self.get_by_idempotency_key(key)
        if existing is not None:
            return existing, False

        record = {
            "request_id": f"REQ-{context.request_id}",
            "created_at": context.timestamp.isoformat(),
            "user_id": context.user_id,
            "user_name": context.user_name,
            "user_email": context.user_email,
            "user_roles_json": canonical_json(list(context.user_roles)),
            "user_groups_json": canonical_json(list(context.user_groups)),
            "user_permissions_json": canonical_json(list(context.user_permissions)),
            "chat_id": context.chat_id,
            "session_id": context.session_id,
            "message_id": context.message_id,
            "model_id": context.model_id,
            "tool_name": tool_name,
            "arguments_json": canonical_json(arguments),
            "arguments_hash": arguments_hash(arguments),
            "idempotency_key": key,
            "policy_version": policy_version,
            "workflow_version": decision.workflow_version,
            "matched_rule": decision.matched_rule,
            "status": "PENDING",
            "approver_group": decision.approver_group,
            "reason": decision.reason,
        }
        columns = list(record)
        placeholders = ", ".join("?" for _ in columns)
        try:
            connection = self._connect()
            try:
                connection.execute(
                    f"INSERT INTO approval_requests ({', '.join(columns)}) VALUES ({placeholders})",
                    [record[column] for column in columns],
                )
                connection.commit()
            finally:
                connection.close()
        except sqlite3.IntegrityError:
            existing = self.get_by_idempotency_key(key)
            if existing is None:
                raise
            return existing, False
        return record, True
