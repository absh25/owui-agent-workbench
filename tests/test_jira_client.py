from __future__ import annotations

from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock


TEST_ROOT = Path(__file__).parents[1]
JIRA_MCP_DIR = next(
    path
    for path in (TEST_ROOT / "lab" / "jira-mcp", TEST_ROOT / "jira-mcp")
    if path.is_dir()
)
sys.path.insert(0, str(JIRA_MCP_DIR))

from jira_client import (
    JiraAPIError,
    JiraClient,
    JiraConfigurationError,
    adf_text,
    normalize_issue,
)


class JiraClientTests(unittest.IsolatedAsyncioTestCase):
    def make_client(self) -> JiraClient:
        return JiraClient("https://example.atlassian.net", "bot@example.test", "token")

    def ready_context(self) -> dict:
        return {
            "status": "ready",
            "project": {"id": "10000", "key": "KAN", "name": "Kanban"},
            "can_create": True,
            "issue_types": [
                {
                    "id": "10003",
                    "name": "任务",
                    "untranslated_name": "Task",
                    "subtask": False,
                },
                {
                    "id": "10004",
                    "name": "故事",
                    "untranslated_name": "Story",
                    "subtask": False,
                },
            ],
        }

    def test_adf_text_preserves_lines(self) -> None:
        document = adf_text("first\nsecond")
        self.assertEqual(document["type"], "doc")
        self.assertEqual(document["content"][1]["content"][0]["text"], "second")

    def test_normalize_issue(self) -> None:
        issue = {
            "id": "10001",
            "key": "DEV-1",
            "fields": {
                "summary": "Example",
                "status": {"name": "To Do"},
                "priority": {"name": "Medium"},
                "assignee": {"accountId": "abc", "displayName": "Alice"},
            },
        }
        normalized = normalize_issue(issue, "https://example.atlassian.net")
        self.assertEqual(normalized["key"], "DEV-1")
        self.assertEqual(normalized["status"], "To Do")
        self.assertEqual(normalized["assignee"]["account_id"], "abc")

    def test_missing_credentials_fail_before_request(self) -> None:
        with self.assertRaises(JiraConfigurationError):
            JiraClient("https://example.atlassian.net", "", "")._validate()

    async def test_create_issue_uses_rest_v3_and_adf(self) -> None:
        client = self.make_client()
        client.get_create_context = AsyncMock(return_value=self.ready_context())
        client._request = AsyncMock(return_value={"id": "1", "key": "KAN-1"})
        result = await client.create_issue(
            project_key="KAN",
            summary="Example",
            description="Details",
            priority="Medium",
        )
        self.assertEqual(result["key"], "KAN-1")
        client.get_create_context.assert_awaited_once_with("KAN")
        args, kwargs = client._request.await_args
        self.assertEqual(args, ("POST", "/rest/api/3/issue"))
        fields = kwargs["json_body"]["fields"]
        self.assertEqual(fields["description"]["type"], "doc")
        self.assertEqual(fields["project"], {"key": "KAN"})
        self.assertEqual(fields["issuetype"], {"id": "10003"})
        self.assertEqual(fields["priority"], {"name": "Medium"})

    async def test_create_context_lists_actual_projects_read_only(self) -> None:
        client = self.make_client()
        client._request = AsyncMock(
            return_value={
                "startAt": 0,
                "maxResults": 50,
                "isLast": True,
                "values": [
                    {"id": "10000", "key": "KAN", "name": "Kanban", "self": "unused"},
                    {"id": "10001", "key": "OPS", "name": "Operations"},
                ],
            }
        )
        result = await client.get_create_context()
        self.assertEqual(
            result,
            {
                "status": "projects_available",
                "projects": [
                    {"id": "10000", "key": "KAN", "name": "Kanban"},
                    {"id": "10001", "key": "OPS", "name": "Operations"},
                ],
                "is_last": True,
                "next_start_at": None,
            },
        )
        client._request.assert_awaited_once_with(
            "GET", "/rest/api/3/project/search", params={"startAt": 0, "maxResults": 50}
        )

    async def test_create_context_exposes_project_pagination(self) -> None:
        client = self.make_client()
        client._request = AsyncMock(
            return_value={
                "startAt": 0,
                "maxResults": 50,
                "isLast": False,
                "values": [
                    {"id": str(index), "key": f"P{index}", "name": f"Project {index}"}
                    for index in range(50)
                ],
            }
        )
        result = await client.get_create_context()
        self.assertFalse(result["is_last"])
        self.assertEqual(result["next_start_at"], 50)
        self.assertEqual(len(result["projects"]), 50)
        self.assertTrue(all(call.args[0] == "GET" for call in client._request.await_args_list))

    async def test_create_context_checks_permission_and_filters_subtasks(self) -> None:
        client = self.make_client()
        project = {"id": "10000", "key": "KAN", "name": "Kanban"}
        client._request = AsyncMock(
            side_effect=[
                {**project, "self": "unused"},
                {"permissions": {"CREATE_ISSUES": {"havePermission": True}}},
                {
                    "issueTypes": [
                        {"id": "10003", "name": "任务", "untranslatedName": "Task", "subtask": False},
                        {"id": "10005", "name": "子任务", "untranslatedName": "Subtask", "subtask": True},
                    ],
                },
            ]
        )
        result = await client.get_create_context("KAN")
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["project"], project)
        self.assertTrue(result["can_create"])
        self.assertEqual(result["issue_types"], self.ready_context()["issue_types"][:1])
        calls = client._request.await_args_list
        self.assertEqual(calls[0].args, ("GET", "/rest/api/3/project/KAN"))
        self.assertEqual(calls[1].args, ("GET", "/rest/api/3/mypermissions"))
        self.assertEqual(
            calls[1].kwargs["params"],
            {"projectKey": "KAN", "permissions": "CREATE_ISSUES"},
        )
        self.assertEqual(calls[2].args, ("GET", "/rest/api/3/issue/createmeta/KAN/issuetypes"))
        self.assertEqual(calls[2].kwargs["params"], {"startAt": 0, "maxResults": 100})
        self.assertTrue(all(call.args[0] == "GET" for call in calls))

    async def test_create_context_permission_denial_skips_metadata(self) -> None:
        client = self.make_client()
        project = {"id": "10000", "key": "KAN", "name": "Kanban"}
        client._request = AsyncMock(
            side_effect=[
                project,
                {"permissions": {"CREATE_ISSUES": {"havePermission": False}}},
            ]
        )
        self.assertEqual(
            await client.get_create_context("KAN"),
            {
                "status": "create_not_permitted",
                "project": project,
                "can_create": False,
                "issue_types": [],
            },
        )
        self.assertEqual(client._request.await_count, 2)

    async def test_create_context_missing_permission_is_not_assumed_granted(self) -> None:
        client = self.make_client()
        client._request = AsyncMock(
            side_effect=[
                {"id": "10000", "key": "KAN", "name": "Kanban"},
                {"permissions": {}},
            ]
        )
        result = await client.get_create_context("KAN")
        self.assertFalse(result["can_create"])
        self.assertEqual(result["status"], "create_not_permitted")
        self.assertEqual(client._request.await_count, 2)

    async def test_create_context_url_encodes_project_key_in_paths(self) -> None:
        client = self.make_client()
        project_key = "OPS / QA"
        client._request = AsyncMock(
            side_effect=[
                {"id": "10000", "key": project_key, "name": "Operations"},
                {"permissions": {"CREATE_ISSUES": {"havePermission": True}}},
                {"issueTypes": []},
            ]
        )
        await client.get_create_context(project_key)
        calls = client._request.await_args_list
        self.assertEqual(calls[0].args, ("GET", "/rest/api/3/project/OPS%20%2F%20QA"))
        self.assertEqual(calls[1].kwargs["params"]["projectKey"], project_key)
        self.assertEqual(
            calls[2].args,
            ("GET", "/rest/api/3/issue/createmeta/OPS%20%2F%20QA/issuetypes"),
        )

    async def test_create_issue_resolves_localized_name_alias_and_id(self) -> None:
        for requested_type, expected_id in (
            ("Task", "10003"),
            ("task", "10003"),
            ("任务", "10003"),
            ("10003", "10003"),
            ("sToRy", "10004"),
        ):
            with self.subTest(issue_type=requested_type):
                client = self.make_client()
                client.get_create_context = AsyncMock(return_value=self.ready_context())
                client._request = AsyncMock(return_value={"id": "1", "key": "KAN-1"})
                await client.create_issue(
                    project_key="KAN", summary="Example", description="Details", issue_type=requested_type
                )
                fields = client._request.await_args.kwargs["json_body"]["fields"]
                self.assertEqual(fields["issuetype"], {"id": expected_id})

    async def test_create_issue_permission_denial_never_posts(self) -> None:
        client = self.make_client()
        context = self.ready_context()
        context.update(status="create_not_permitted", can_create=False, issue_types=[])
        client.get_create_context = AsyncMock(return_value=context)
        client._request = AsyncMock()
        with self.assertRaises((ValueError, JiraAPIError)):
            await client.create_issue(project_key="KAN", summary="Example", description="Details")
        client._request.assert_not_awaited()

    async def test_create_issue_unavailable_type_never_posts(self) -> None:
        client = self.make_client()
        client.get_create_context = AsyncMock(return_value=self.ready_context())
        client._request = AsyncMock()
        with self.assertRaises((ValueError, JiraAPIError)):
            await client.create_issue(
                project_key="KAN", summary="Example", description="Details", issue_type="Bug"
            )
        client._request.assert_not_awaited()

    async def test_create_issue_invalid_project_propagates_before_post(self) -> None:
        client = self.make_client()
        failure = JiraAPIError("Project does not exist or is inaccessible")
        client._request = AsyncMock(side_effect=failure)
        with self.assertRaises(JiraAPIError) as raised:
            await client.create_issue(project_key="UNKNOWN", summary="Example", description="Details")
        self.assertIs(raised.exception, failure)
        client._request.assert_awaited_once_with("GET", "/rest/api/3/project/UNKNOWN")

    async def test_create_issue_blank_project_has_no_default_fallback(self) -> None:
        client = self.make_client()
        client.get_create_context = AsyncMock(
            return_value={"status": "projects_available", "projects": [], "is_last": True, "next_start_at": None}
        )
        client._request = AsyncMock()
        with self.assertRaises((ValueError, JiraAPIError)):
            await client.create_issue(project_key="", summary="Example", description="Details")
        client._request.assert_not_awaited()

    async def test_transition_resolves_name_to_id(self) -> None:
        client = JiraClient("https://example.atlassian.net", "bot@example.test", "token")
        client._request = AsyncMock(
            side_effect=[
                {"transitions": [{"id": "31", "name": "Done"}]},
                None,
            ]
        )
        result = await client.transition_issue("DEV-1", "done")
        self.assertEqual(result["transition_id"], "31")
        second = client._request.await_args_list[1]
        self.assertEqual(second.args, ("POST", "/rest/api/3/issue/DEV-1/transitions"))
        self.assertEqual(second.kwargs["json_body"], {"transition": {"id": "31"}})


if __name__ == "__main__":
    unittest.main()
