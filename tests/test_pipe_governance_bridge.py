from __future__ import annotations

from pathlib import Path
import tempfile
import textwrap
import unittest

from governance import ToolExecutionContext, ToolMiddleware, govern_owui_tools


class PipeGovernanceBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_wrapped_callable_blocks_before_original(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy = root / "tools.yaml"
            workflow = root / "workflow.yaml"
            database = root / "governance.sqlite3"
            policy.write_text(
                textwrap.dedent(
                    """
                    version: "1"
                    defaults:
                      mode: passthrough
                    tools:
                      dangerous_tool:
                        mode: governed
                        workflow: jira
                    """
                ),
                encoding="utf-8",
            )
            workflow.write_text(
                textwrap.dedent(
                    """
                    version: "1"
                    default:
                      decision: review
                      approver_group: reviewers
                    rules: []
                    """
                ),
                encoding="utf-8",
            )
            middleware = ToolMiddleware(
                policy_path=policy,
                workflow_paths={"jira": workflow},
                database_path=database,
            )
            calls = []

            async def original(**arguments):
                calls.append(arguments)
                return "executed"

            tools = {
                "dangerous_tool": {
                    "callable": original,
                    "direct": False,
                    "spec": {
                        "description": "test",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            }
            wrapped = govern_owui_tools(
                tools,
                middleware=middleware,
                base_context=ToolExecutionContext(user_id="user-1", chat_id="chat-1"),
                event_call=None,
                metadata={},
            )
            result = await wrapped["dangerous_tool"]["callable"](value=1)
            self.assertEqual(result["status"], "WAITING_FOR_APPROVAL")
            self.assertEqual(calls, [])
            self.assertFalse(wrapped["dangerous_tool"]["direct"])

    async def test_direct_browser_tool_is_also_wrapped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy = root / "tools.yaml"
            workflow = root / "workflow.yaml"
            database = root / "governance.sqlite3"
            policy.write_text(
                "version: '1'\ndefaults:\n  mode: passthrough\ntools: {}\n",
                encoding="utf-8",
            )
            workflow.write_text(
                "version: '1'\ndefault:\n  decision: review\nrules: []\n",
                encoding="utf-8",
            )
            middleware = ToolMiddleware(
                policy_path=policy,
                workflow_paths={"jira": workflow},
                database_path=database,
            )
            events = []

            async def event_call(event):
                events.append(event)
                return {"ok": True}

            wrapped = govern_owui_tools(
                {
                    "browser_tool": {
                        "direct": True,
                        "server": {"id": "browser"},
                        "spec": {"parameters": {"type": "object", "properties": {}}},
                    }
                },
                middleware=middleware,
                base_context=ToolExecutionContext(user_id="user-1", chat_id="chat-1"),
                event_call=event_call,
                metadata={"session_id": "session-1"},
            )
            result = await wrapped["browser_tool"]["callable"](query="test")
            self.assertEqual(result, {"ok": True})
            self.assertEqual(events[0]["type"], "execute:tool")
            self.assertEqual(events[0]["data"]["params"], {"query": "test"})


if __name__ == "__main__":
    unittest.main()
