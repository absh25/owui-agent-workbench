import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from supervisor.native_pipe import Pipe, convert_messages, protect_tools, current_retrieval_status
from supervisor.coding_tool import project_directory, structured_result


def fake_api():
    api = AsyncMock()
    api.is_closed = Mock(return_value=False)
    return api


class SupervisorTests(unittest.IsolatedAsyncioTestCase):
    def test_retrieval_gate_uses_current_observed_turn_and_ignores_old_failures(self):
        history = [
            {"role": "user", "content": "先查证再编码"},
            {"role": "assistant", "tool_calls": [{"id": "r1", "function": {"name": "delegate_retrieval_task"}}]},
            {"role": "tool", "tool_call_id": "r1", "content": '{"status":"insufficient_evidence"}'},
        ]
        self.assertEqual(current_retrieval_status(history), "insufficient_evidence")
        self.assertIsNone(current_retrieval_status(history + [{"role": "user", "content": "现在修复另一项代码错误"}]))
        history[-1]["tool_call_id"] = "other-tool"
        self.assertIsNone(current_retrieval_status(history))

    async def test_insufficient_or_failed_evidence_blocks_coding_and_repeat_delegation(self):
        from types import SimpleNamespace as NS
        for outcome in ('{"status":"insufficient_evidence"}', '{"status":"failed"}', 'invalid JSON'):
            with self.subTest(outcome=outcome):
                api = fake_api()
                api.messages.create.return_value = NS(content=[NS(type="text", text="需要补充资料")])
                tools = {name: {"supervisor_governed": True} for name in
                         ("delegate_retrieval_task", "delegate_coding_task", "search_jira")}
                body = {"stream": False, "messages": [
                    {"role": "user", "content": "查证后写代码"},
                    {"role": "assistant", "tool_calls": [{"id": "r1", "function": {
                        "name": "delegate_retrieval_task", "arguments": '{}'}}]},
                    {"role": "tool", "tool_call_id": "r1", "content": outcome},
                ], "tools": [{"type": "function", "function": {"name": name,
                    "parameters": {"type": "object", "properties": {}}}} for name in tools]}
                with patch("supervisor.native_pipe.AsyncAnthropic", return_value=api):
                    await Pipe().pipe(body, __tools__=tools)
                self.assertEqual(set(tools), {"search_jira"})
                self.assertEqual([t["name"] for t in api.messages.create.call_args.kwargs["tools"]], ["search_jira"])

    def test_native_tool_history_preserves_results_and_ids(self):
        _, messages = convert_messages([
            {"role": "user", "content": "查询 Jira"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "call-1", "function": {
                "name": "search_jira", "arguments": '{"jql":"project=KAN"}'}}]},
            {"role": "tool", "tool_call_id": "call-1", "content": '{"total":1}'},
        ])
        self.assertEqual(messages[1]["content"][0]["input"], {"jql": "project=KAN"})
        self.assertEqual(messages[2]["content"][0]["tool_use_id"], "call-1")

    async def test_native_business_call_waits_and_cancel_blocks_changed_retry(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(__file__).parents[1]
            env = {"TOOL_GOVERNANCE_DATABASE": str(Path(temp) / "approval.db"),
                   "TOOL_GOVERNANCE_POLICY": str(root / "policies/tool-governance.yaml"),
                   "TOOL_GOVERNANCE_JIRA_WORKFLOW": str(root / "policies/jira-workflow.yaml")}
            writer, entered = AsyncMock(), asyncio.Event()
            answer = asyncio.get_running_loop().create_future()
            async def confirm(event):
                entered.set()
                return await answer
            tools = {"create_jira_ticket": {"callable": writer}}
            with patch.dict("os.environ", env):
                protect_tools(tools, {"id": "user-1"}, {"chat_id": "chat-1", "message_id": "msg-1"}, confirm, None)
                original_wrapper = tools["create_jira_ticket"]["callable"]
                # Native followups use the same wrapper and cancellation state.
                protect_tools(tools, {"id": "user-1"}, {"chat_id": "chat-1"}, confirm, None)
                self.assertIs(original_wrapper, tools["create_jira_ticket"]["callable"])
                task = asyncio.create_task(original_wrapper(project_key="KAN", summary="预览"))
                await entered.wait()
                writer.assert_not_awaited()
                answer.set_result(False)
                self.assertEqual((await task)["status"], "CANCELLED")
                self.assertEqual((await original_wrapper(project_key="KAN", summary="改参数"))["status"], "CANCELLED")
                writer.assert_not_awaited()

    def test_repo_must_stay_in_current_users_terminal_workspace(self):
        self.assertEqual(project_directory("repo", "/workspace/user"), "/workspace/user/repo")
        for repo in ("../other", "/app/backend", "/workspace/user-other"):
            with self.assertRaises(ValueError):
                project_directory(repo, "/workspace/user")

    def test_missing_result_does_not_claim_success(self):
        result = structured_result(None, "task-1", "/workspace/user", [])
        self.assertEqual(result["status"], "failed")
        self.assertNotIn("passed", str(result))

    def test_compatible_provider_json_fence_with_preface_is_structured(self):
        from types import SimpleNamespace
        result = structured_result(SimpleNamespace(is_error=False, subtype="success", result=
            '任务完成：\n```json\n{"summary":"完成", "changed_files":["app.py"], "tests":[]}\n```'),
            "task-1", "/workspace/user", [])
        self.assertEqual(result["changed_files"], ["app.py"])
        self.assertEqual(result["status"], "verification_failed")
        self.assertIn("验收未通过", result["summary"])

    def test_plain_agent_answer_keeps_observed_changes_and_command_exit_codes(self):
        from types import SimpleNamespace
        evidence = [
            {"tool": "write_file", "arguments": {"path": "app.py"}, "result": "written", "is_error": False},
            {"tool": "run_command", "arguments": {"command": "python -m unittest"},
             "result": "Ran 2 tests; OK", "is_error": False, "exit_code": 0, "process_status": "done"},
        ]
        result = structured_result(SimpleNamespace(is_error=False, subtype="success", result="完成"),
                                   "task-1", "/workspace/user", evidence)
        self.assertEqual(result["changed_files"], ["app.py"])
        self.assertEqual(result["tests"]["command_results"][0]["exit_code"], 0)
        self.assertIsNone(result["tests"]["report"])

    async def test_supervisor_emits_tool_call_without_executing_tool(self):
        from types import SimpleNamespace as NS
        api, execute = fake_api(), AsyncMock()
        api.messages.create.return_value = NS(content=[NS(type="tool_use", id="call-1",
            name="delegate_coding_task", input={"task": "修复代码"})])
        tools = {"delegate_coding_task": {"callable": execute, "supervisor_governed": True}}
        body = {"stream": False, "messages": [{"role": "user", "content": "修复代码"}],
                "tools": [{"type": "function", "function": {"name": "delegate_coding_task",
                    "parameters": {"type": "object", "properties": {}}}}]}
        with patch("supervisor.native_pipe.AsyncAnthropic", return_value=api):
            result = await Pipe().pipe(body, __tools__=tools)
        self.assertEqual(result["choices"][0]["finish_reason"], "tool_calls")
        self.assertEqual(result["choices"][0]["message"]["tool_calls"][0]["function"]["name"], "delegate_coding_task")
        execute.assert_not_awaited()

    async def test_specialist_tools_are_removed_from_native_execution_map(self):
        from types import SimpleNamespace as NS
        api = fake_api()
        api.messages.create.return_value = NS(content=[NS(type="text", text="已委派")])
        tools = {
            "delegate_retrieval_task": {"supervisor_governed": True},
            "ragflow-knowledge_search_knowledge": {"tool_id": "server:mcp:ragflow-knowledge"},
            "read_file": {"type": "terminal"},
            "execute_code": {},
            "grep_knowledge_files": {},
            "query_knowledge_files": {},
            "list_knowledge_bases": {},
            "kb_exec": {},
        }
        body = {"stream": False, "messages": [{"role": "user", "content": "查询资料"}],
                "tools": [{"type": "function", "function": {"name": name,
                    "parameters": {"type": "object", "properties": {}}}} for name in tools]}
        with patch("supervisor.native_pipe.AsyncAnthropic", return_value=api):
            await Pipe().pipe(body, __tools__=tools)
        self.assertEqual(set(tools), {"delegate_retrieval_task"})
        definitions = api.messages.create.call_args.kwargs["tools"]
        self.assertEqual([t["name"] for t in definitions], ["delegate_retrieval_task"])

    async def test_transport_pool_is_reused_without_sharing_chat_messages(self):
        from types import SimpleNamespace as NS
        api = fake_api()
        api.messages.create.return_value = NS(content=[NS(type="text", text="完成")])
        pipe = Pipe()
        with patch("supervisor.native_pipe.AsyncAnthropic", return_value=api) as factory:
            for content in ("用户 A 的任务", "用户 B 的任务"):
                await pipe.pipe({"stream": False, "messages": [{"role": "user", "content": content}]})
        factory.assert_called_once()
        api.close.assert_not_awaited()
        histories = [call.kwargs["messages"] for call in api.messages.create.await_args_list]
        self.assertEqual(histories[0], [{"role": "user", "content": [{"type": "text", "text": "用户 A 的任务"}]}])
        self.assertEqual(histories[1], [{"role": "user", "content": [{"type": "text", "text": "用户 B 的任务"}]}])

    async def test_provider_configuration_change_replaces_and_closes_idle_client(self):
        first, second = fake_api(), fake_api()
        pipe = Pipe()
        with patch("supervisor.native_pipe.AsyncAnthropic", side_effect=[first, second]) as factory:
            with patch.dict("os.environ", {"ANTHROPIC_BASE_URL": "https://provider-a.invalid"}):
                async with pipe._use_client() as client:
                    self.assertIs(client, first)
            with patch.dict("os.environ", {"ANTHROPIC_BASE_URL": "https://provider-b.invalid"}):
                async with pipe._use_client() as client:
                    self.assertIs(client, second)
        self.assertEqual(factory.call_count, 2)
        first.close.assert_awaited_once()
        second.close.assert_not_awaited()

    async def test_closed_transport_is_replaced(self):
        first, second = fake_api(), fake_api()
        pipe = Pipe()
        with patch("supervisor.native_pipe.AsyncAnthropic", side_effect=[first, second]) as factory:
            async with pipe._use_client():
                pass
            first.is_closed.return_value = True
            async with pipe._use_client() as client:
                self.assertIs(client, second)
        self.assertEqual(factory.call_count, 2)
        first.close.assert_not_awaited()

    async def test_configuration_refresh_waits_for_old_active_request_before_closing(self):
        first, second = fake_api(), fake_api()
        pipe = Pipe()
        with patch("supervisor.native_pipe.AsyncAnthropic", side_effect=[first, second]):
            with patch.dict("os.environ", {"ANTHROPIC_BASE_URL": "https://provider-a.invalid"}):
                async with pipe._use_client() as active:
                    with patch.dict("os.environ", {"ANTHROPIC_BASE_URL": "https://provider-b.invalid"}):
                        async with pipe._use_client() as replacement:
                            self.assertIs(active, first)
                            self.assertIs(replacement, second)
                            first.close.assert_not_awaited()
                    first.close.assert_not_awaited()
        first.close.assert_awaited_once()
        second.close.assert_not_awaited()

    async def test_streaming_rounds_reuse_the_same_transport(self):
        from types import SimpleNamespace as NS
        api = fake_api()

        async def events():
            yield NS(type="content_block_delta", delta=NS(type="text_delta", text="完成"))
            yield NS(type="message_delta", delta=NS(stop_reason="end_turn"))

        def stream_context(**kwargs):
            context = AsyncMock()
            context.__aenter__.return_value = events()
            return context

        api.messages.stream = Mock(side_effect=stream_context)
        pipe = Pipe()
        with patch("supervisor.native_pipe.AsyncAnthropic", return_value=api) as factory:
            for content in ("第一轮", "第二轮"):
                response = await pipe.pipe({"stream": True, "messages": [{"role": "user", "content": content}]})
                output = "".join([item async for item in response.body_iterator])
                self.assertIn("完成", output)
                self.assertIn("[DONE]", output)
        factory.assert_called_once()
        self.assertEqual(api.messages.stream.call_count, 2)
        api.close.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
