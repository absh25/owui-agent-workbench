from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import tempfile
import textwrap
import unittest

from governance import ToolExecutionContext, ToolMiddleware
from governance.approval_store import arguments_hash
from governance.audit import redact


class ToolMiddlewareTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.database = root / "governance.sqlite3"
        self.policy = root / "tool-governance.yaml"
        self.workflow = root / "jira-workflow.yaml"
        self.policy.write_text(
            textwrap.dedent(
                """
                version: "test-policy"
                defaults:
                  mode: passthrough
                tools:
                  delete_jira_ticket:
                    mode: deny
                    reason: deletion prohibited
                  transition_jira_ticket:
                    mode: governed
                    workflow: jira
                  create_jira_ticket:
                    mode: governed
                    workflow: jira
                patterns:
                  - match: "*transition_jira_ticket"
                    mode: governed
                    workflow: jira
                """
            ),
            encoding="utf-8",
        )
        self.workflow.write_text(
            textwrap.dedent(
                """
                version: "test-workflow"
                default:
                  decision: review
                  approver_group: fallback-approvers
                rules:
                  - id: close-review
                    tool: transition_jira_ticket
                    arguments:
                      target_status:
                        in: [Done, Closed]
                    decision: review
                    reason: completion requires review
                    approver_group: project-approvers
                  - id: normal-auto
                    tool: create_jira_ticket
                    arguments:
                      priority:
                        in: [low, medium]
                    decision: auto
                """
            ),
            encoding="utf-8",
        )
        self.middleware = ToolMiddleware(
            policy_path=self.policy,
            workflow_paths={"jira": self.workflow},
            database_path=self.database,
        )
        self.context = ToolExecutionContext(
            user_id="real-user",
            user_name="Real User",
            user_email="real@example.test",
            user_roles=("user",),
            chat_id="chat-1",
            session_id="session-1",
            message_id="message-1",
            model_id="test-model",
            user_groups=("wenguang",),
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    async def test_passthrough_executes_original_callable(self) -> None:
        calls = []

        async def original(**arguments):
            calls.append(arguments)
            return {"issues": ["DEV-1"]}

        result = await self.middleware.execute(
            tool_name="search_jira",
            arguments={"query": "open"},
            original_callable=original,
            context=self.context.for_tool("search_jira"),
        )
        self.assertEqual(result, {"issues": ["DEV-1"]})
        self.assertEqual(calls, [{"query": "open"}])

    async def test_deny_never_executes_original_callable(self) -> None:
        called = False

        async def original(**arguments):
            nonlocal called
            called = True

        result = await self.middleware.execute(
            tool_name="delete_jira_ticket",
            arguments={"issue_key": "DEV-1"},
            original_callable=original,
            context=self.context.for_tool("delete_jira_ticket"),
        )
        self.assertFalse(called)
        self.assertEqual(result["status"], "DENIED")
        self.assertFalse(result["retry_allowed"])

    async def test_review_persists_request_without_execution(self) -> None:
        called = False

        async def original(**arguments):
            nonlocal called
            called = True

        result = await self.middleware.execute(
            tool_name="transition_jira_ticket",
            arguments={"issue_key": "DEV-100", "target_status": "Done"},
            original_callable=original,
            context=self.context.for_tool("transition_jira_ticket"),
        )
        self.assertFalse(called)
        self.assertEqual(result["status"], "WAITING_FOR_APPROVAL")
        self.assertEqual(result["approver_group"], "project-approvers")
        connection = sqlite3.connect(self.database)
        try:
            approval_count = connection.execute(
                "SELECT COUNT(*) FROM approval_requests"
            ).fetchone()[0]
            audit_count = connection.execute(
                "SELECT COUNT(*) FROM audit_logs"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(approval_count, 1)
        self.assertEqual(audit_count, 1)

    async def test_current_group_admin_target_is_resolved(self) -> None:
        self.workflow.write_text(
            textwrap.dedent(
                """
                version: test
                default:
                  decision: review
                  approver_group: current_user_group_admin
                """
            ), encoding="utf-8"
        )
        result = await self.middleware.execute(
            tool_name="transition_jira_ticket",
            arguments={"issue_key": "DEV-101", "target_status": "In Progress"},
            original_callable=lambda **_: {"unexpected": True},
            context=self.context.for_tool("transition_jira_ticket"),
        )
        self.assertEqual(result["approver_group"], "group-admins:wenguang")
        self.assertEqual(
            len(self.middleware.approvals.list_pending_for_groups(["wenguang"])), 1
        )

    async def test_review_creates_jira_approval_issue(self) -> None:
        issued = []

        self.workflow.write_text(
            textwrap.dedent(
                """
                version: test
                default:
                  decision: review
                  approver_group: current_user_group_admin
                """
            ), encoding="utf-8"
        )

        async def issue_creator(**arguments):
            issued.append(arguments)
            return {"key": "OWUI-42", "url": "https://jira.example/browse/OWUI-42"}

        middleware = ToolMiddleware(
            policy_path=self.policy,
            workflow_paths={"jira": self.workflow},
            database_path=self.database,
            approval_issue_callable=issue_creator,
            approval_project_key="OWUI",
            group_admin_account_ids={"wenguang": "account-42"},
        )
        result = await middleware.execute(
            tool_name="transition_jira_ticket",
            arguments={"issue_key": "DEV-102", "target_status": "Done"},
            original_callable=lambda **_: self.fail("business operation must not run"),
            context=self.context.for_tool("transition_jira_ticket"),
        )
        self.assertEqual(result["approval_issue"]["key"], "OWUI-42")
        self.assertEqual(issued[0]["assignee_account_id"], "account-42")

    async def test_prefixed_mcp_tool_name_is_governed(self) -> None:
        called = False

        async def original(**arguments):
            nonlocal called
            called = True

        result = await self.middleware.execute(
            tool_name="company-jira_transition_jira_ticket",
            arguments={"issue_key": "DEV-100", "target_status": "Done"},
            original_callable=original,
            context=self.context.for_tool("company-jira_transition_jira_ticket"),
        )
        self.assertFalse(called)
        self.assertEqual(result["status"], "WAITING_FOR_APPROVAL")

    async def test_auto_executes_original_callable(self) -> None:
        calls = []

        def original(**arguments):
            calls.append(arguments)
            return {"key": "TEST-1"}

        result = await self.middleware.execute(
            tool_name="create_jira_ticket",
            arguments={"project_key": "TEST", "priority": "medium"},
            original_callable=original,
            context=self.context.for_tool("create_jira_ticket"),
        )
        self.assertEqual(result, {"key": "TEST-1"})
        self.assertEqual(len(calls), 1)

    async def test_duplicate_review_returns_same_request(self) -> None:
        async def original(**arguments):
            raise AssertionError("reviewed callable must not execute")

        arguments = {"issue_key": "DEV-100", "target_status": "Done"}
        first = await self.middleware.execute(
            tool_name="transition_jira_ticket",
            arguments=arguments,
            original_callable=original,
            context=self.context.for_tool("transition_jira_ticket"),
        )
        second = await self.middleware.execute(
            tool_name="transition_jira_ticket",
            arguments=arguments,
            original_callable=original,
            context=self.context.for_tool("transition_jira_ticket"),
        )
        self.assertEqual(first["request_id"], second["request_id"])
        self.assertTrue(second["duplicate"])
        connection = sqlite3.connect(self.database)
        try:
            count = connection.execute(
                "SELECT COUNT(*) FROM approval_requests"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(count, 1)

    async def test_model_cannot_spoof_server_identity(self) -> None:
        arguments = {
            "issue_key": "DEV-100",
            "target_status": "Done",
            "user_id": "root",
            "role": "admin",
        }

        async def original(**arguments):
            raise AssertionError("reviewed callable must not execute")

        await self.middleware.execute(
            tool_name="transition_jira_ticket",
            arguments=arguments,
            original_callable=original,
            context=self.context.for_tool("transition_jira_ticket"),
        )
        connection = sqlite3.connect(self.database)
        try:
            row = connection.execute(
                "SELECT user_id, arguments_json FROM approval_requests"
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(row[0], "real-user")
        self.assertEqual(json.loads(row[1])["user_id"], "root")

    def test_argument_hash_is_canonical(self) -> None:
        self.assertEqual(
            arguments_hash({"b": 2, "a": 1}),
            arguments_hash({"a": 1, "b": 2}),
        )

    def test_audit_redacts_nested_credentials(self) -> None:
        self.assertEqual(
            redact(
                {
                    "api_token": "secret-value",
                    "nested": {"Authorization": "Bearer abc", "safe": "ok"},
                }
            ),
            {
                "api_token": "[REDACTED]",
                "nested": {"Authorization": "[REDACTED]", "safe": "ok"},
            },
        )


if __name__ == "__main__":
    unittest.main()
