from __future__ import annotations

import unittest
import sys
import types
import tempfile
import asyncio
import time
from unittest.mock import patch
from contextlib import ExitStack


try:
    import claude_agent_sdk  # noqa: F401
except ModuleNotFoundError:
    sdk = types.ModuleType("claude_agent_sdk")

    class _Message:
        pass

    sdk.AssistantMessage = _Message
    sdk.ClaudeAgentOptions = _Message
    sdk.ClaudeSDKClient = _Message
    sdk.ResultMessage = _Message
    sdk.StreamEvent = _Message
    sdk.SystemMessage = _Message
    sdk.TextBlock = _Message
    sdk.ThinkingBlock = _Message
    sdk.ToolResultBlock = _Message
    sdk.ToolUseBlock = _Message
    sdk.UserMessage = _Message
    sdk.create_sdk_mcp_server = lambda *args, **kwargs: None
    sdk.tool = lambda *args, **kwargs: (lambda function: function)
    sys.modules["claude_agent_sdk"] = sdk

from claude_agent_pipe import (
    Pipe,
    _LiveProgress,
    _ToolFollowupTimeout,
    _is_fast_read_only_tool,
    _agent_session_key,
    _prior_owui_context,
    _model_error_description,
    _is_capability_only_prompt,
    _is_social_only_prompt,
    _with_silence_timeout,
)


class PromptRoutingTests(unittest.TestCase):
    def test_history_bootstrap_excludes_current_prompt_and_display_cards(self) -> None:
        body = {"messages": [
            {"role": "system", "content": "long-term preference"},
            {"role": "user", "content": "项目叫月光"},
            {"role": "assistant", "content": "<details>old tool input</details>记住了。"},
            {"role": "user", "content": "项目叫什么？"},
        ]}
        context = _prior_owui_context(body)
        self.assertIn("月光", context)
        self.assertIn("记住了", context)
        self.assertNotIn("old tool input", context)
        self.assertNotIn("项目叫什么", context)
        self.assertNotIn("long-term preference", context)

    def test_session_key_separates_users_and_chats(self) -> None:
        self.assertNotEqual(_agent_session_key({"id": "a"}, "chat"), _agent_session_key({"id": "b"}, "chat"))
        self.assertNotEqual(_agent_session_key({"id": "a"}, "chat1"), _agent_session_key({"id": "a"}, "chat2"))

    def test_balance_error_has_actionable_chinese_description(self) -> None:
        self.assertIn("余额不足", _model_error_description("402 30001 account balance is insufficient"))

    def test_standalone_greetings_do_not_need_tools(self) -> None:
        for prompt in ("你好", "您好！", "嗨～", "hello", "Hi!", "谢谢", "再见"):
            with self.subTest(prompt=prompt):
                self.assertTrue(_is_social_only_prompt(prompt))

    def test_actionable_prompts_still_receive_tools(self) -> None:
        for prompt in (
            "你好，帮我创建一个 HTML 文件",
            "在吗？检查一下服务",
            "谢谢，接着完成刚才的任务",
            "hello, create a file",
        ):
            with self.subTest(prompt=prompt):
                self.assertFalse(_is_social_only_prompt(prompt))

    def test_capability_questions_do_not_execute_tools(self) -> None:
        for prompt in ("你可以做啥啊", "你能做什么？", "What can you do?"):
            with self.subTest(prompt=prompt):
                self.assertTrue(_is_capability_only_prompt(prompt))
        self.assertFalse(_is_capability_only_prompt("你可以帮我创建一个工单吗？"))


class PlainChatRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_names_stop_and_failed_or_pending_calls_never_claim_success(self) -> None:
        import claude_agent_pipe as module

        class Message:
            def __init__(self, **values):
                self.__dict__.update(values)

        classes = {name: type(name, (Message,), {}) for name in (
            "AssistantMessage", "StreamEvent", "SystemMessage", "ResultMessage",
            "UserMessage", "ToolResultBlock", "ToolUseBlock",
        )}
        for scenario in ("repeated_invalid", "failed", "pending", "streaming"):
            with self.subTest(scenario=scenario):
                interrupted, options, events = [], [], []

                class Client:
                    def __init__(self, **kwargs):
                        options.append(kwargs["options"])

                    async def __aenter__(self):
                        return self

                    async def __aexit__(self, *args):
                        pass

                    async def query(self, prompt):
                        pass

                    async def interrupt(self):
                        interrupted.append(True)

                    async def receive_response(self):
                        if scenario == "streaming":
                            yield classes["StreamEvent"](event={"type": "content_block_start",
                                "content_block": {"type": "tool_use", "name": "mcp__openwebui__run_command"}})
                            for _ in range(8):
                                await asyncio.sleep(0.01)
                                yield classes["StreamEvent"](event={"type": "content_block_delta",
                                    "delta": {"type": "input_json_delta", "partial_json": "html content"}})
                            yield classes["AssistantMessage"](content=[classes["ToolUseBlock"](
                                id="stream", name="mcp__openwebui__run_command", input={"command": "pwd"},
                            )])
                            yield classes["UserMessage"](content=[classes["ToolResultBlock"](
                                tool_use_id="stream", is_error=False, content="/workspace",
                            )])
                            yield classes["ResultMessage"](result="已验证文件，内容完整且读取成功。", subtype="success",
                                is_error=False, total_cost_usd=None, duration_ms=80)
                            return
                        for index in range(2 if scenario == "repeated_invalid" else 1):
                            yield classes["AssistantMessage"](content=[classes["ToolUseBlock"](
                                id=str(index), name="run_command", input={"command": "pwd"},
                            )])
                            if scenario != "pending":
                                yield classes["UserMessage"](content=[classes["ToolResultBlock"](
                                    tool_use_id=str(index), is_error=True,
                                    content="No such tool available: run_command",
                                )])
                        await asyncio.sleep(1)

                async def emit(event):
                    events.append(event["data"])

                with tempfile.TemporaryDirectory() as workdir, ExitStack() as stack:
                    for name, cls in classes.items():
                        stack.enter_context(patch.object(module, name, cls))
                    stack.enter_context(patch.object(module, "ClaudeSDKClient", Client))
                    stack.enter_context(patch.object(module, "ClaudeAgentOptions", lambda **kwargs: kwargs))
                    stack.enter_context(patch.object(module, "_build_owui_mcp_server", return_value=(
                        None, ["mcp__openwebui__run_command"], {"mcp__openwebui__run_command": "run_command"},
                    )))
                    pipe = Pipe()
                    pipe.valves.WORKDIR_ROOT = workdir
                    pipe.valves.TOOL_GOVERNANCE_ENABLED = False
                    pipe.valves.MODEL_SILENCE_TIMEOUT_SECONDS = 0.03
                    pipe.valves.MODEL_INITIAL_RESPONSE_TIMEOUT_SECONDS = 0.02 if scenario == "streaming" else 1
                    pipe.valves.TOOL_FOLLOWUP_TIMEOUT_SECONDS = 0.02
                    output = "".join([chunk async for chunk in pipe.pipe(
                        {"messages": [{"role": "user", "content": "执行 pwd"}]},
                        __chat_id__=f"invalid-{scenario}",
                        __tools__={"run_command": {"type": "terminal", "callable": lambda: None,
                            "spec": {"description": "The current working directory is: /workspace/test-user"}}},
                        __event_emitter__=emit,
                    )])
                self.assertIn("这些工具：mcp__openwebui__run_command", options[0]["system_prompt"])
                self.assertIn("OpenTerminal 沙箱实际工作目录为 /workspace/test-user", options[0]["system_prompt"])
                self.assertNotIn("工具已经执行", output)
                self.assertNotIn("工具已执行", output)
                self.assertTrue(events[-1]["done"])
                if scenario == "streaming":
                    self.assertIn("已验证文件", output)
                    self.assertNotIn("本轮已停止", output)
                    self.assertTrue(any("尚未执行" in event["description"] for event in events))
                elif scenario == "repeated_invalid":
                    self.assertEqual(interrupted, [True])
                    self.assertIn("已停止本轮", output)
                else:
                    self.assertIn("尚未确认成功", output)

    async def test_sdk_seeds_history_once_and_resumes_native_context(self) -> None:
        import claude_agent_pipe as module

        class Message:
            def __init__(self, **values):
                self.__dict__.update(values)

        classes = {name: type(name, (Message,), {}) for name in (
            "AssistantMessage", "StreamEvent", "SystemMessage", "ResultMessage",
            "UserMessage", "ToolResultBlock", "ToolUseBlock",
        )}
        queries, options = [], []

        class Client:
            def __init__(self, **kwargs):
                options.append(kwargs["options"])

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def query(self, prompt):
                queries.append(prompt)

            async def receive_response(self):
                yield classes["SystemMessage"](subtype="init", data={"session_id": "native-session"})
                yield classes["AssistantMessage"](content=[
                    classes["ToolUseBlock"](id="echo-1", name="echo", input={"text": "月光"})
                ])
                yield classes["UserMessage"](content=[
                    classes["ToolResultBlock"](tool_use_id="echo-1", content="月光", is_error=False)
                ])
                yield classes["ResultMessage"](
                    result="项目名称是月光。", subtype="success", is_error=False,
                    total_cost_usd=None, duration_ms=10,
                )

        with tempfile.TemporaryDirectory() as workdir, ExitStack() as stack:
            for name, cls in classes.items():
                stack.enter_context(patch.object(module, name, cls))
            stack.enter_context(patch.object(module, "ClaudeSDKClient", Client))
            stack.enter_context(patch.object(module, "ClaudeAgentOptions", lambda **kwargs: kwargs))
            bridge = stack.enter_context(patch.object(module, "_build_owui_mcp_server", return_value=(None, ["echo"], {"echo": "echo"})))
            pipe = Pipe()
            pipe.valves.WORKDIR_ROOT = workdir
            pipe.valves.TOOL_GOVERNANCE_ENABLED = False
            body = {"messages": [
                {"role": "system", "content": "[User Memory] 用户偏好中文。"},
                {"role": "user", "content": "项目叫月光"},
                {"role": "assistant", "content": "记住了。"},
                {"role": "user", "content": "查询项目名称"},
            ]}
            arguments = {"__chat_id__": "memory-test", "__user__": {"id": "memory-user"},
                         "__tools__": {
                             "echo": {"callable": lambda: None, "spec": {}},
                             "search_memories": {"type": "builtin", "callable": lambda: None, "spec": {}},
                             "unrelated_builtin": {"type": "builtin", "callable": lambda: None, "spec": {}},
                         }}
            for _ in range(2):
                chunks = [chunk async for chunk in pipe.pipe(body, **arguments)]
                self.assertNotIn("上游模型请求失败", "".join(chunks))

            async def plain_response(self, *args, **kwargs):
                yield "你好！"

            greeted = {"messages": [*body["messages"],
                {"role": "assistant", "content": "项目名称是月光。"},
                {"role": "user", "content": "你好"},
            ]}
            with patch.object(Pipe, "_run_messages_api", plain_response):
                _ = [chunk async for chunk in pipe.pipe(greeted, **arguments)]
            after_greeting = {"messages": [*greeted["messages"],
                {"role": "assistant", "content": "你好！"},
                {"role": "user", "content": "继续检查项目"},
            ]}
            _ = [chunk async for chunk in pipe.pipe(after_greeting, **arguments)]
            bridged_tools = bridge.call_args.args[0]
            key = _agent_session_key(arguments["__user__"], "memory-test")
            module._forget_agent_session(key)

        self.assertIn("项目叫月光", queries[0])
        self.assertNotIn("项目叫月光", queries[1])
        self.assertEqual(queries[1], "查询项目名称")
        self.assertNotIn("resume", options[0])
        self.assertEqual(options[1]["resume"], "native-session")
        self.assertIn("[User Memory]", options[0]["system_prompt"])
        self.assertEqual(options[2]["resume"], "native-session")
        self.assertIn("你好", queries[2])
        self.assertNotIn("项目叫月光", queries[2])
        self.assertIn("search_memories", bridged_tools)
        self.assertNotIn("unrelated_builtin", bridged_tools)

    async def test_busy_model_after_tool_uses_summary_without_repeating_action(self) -> None:
        import claude_agent_pipe as module

        class Message:
            def __init__(self, **values):
                self.__dict__.update(values)

        classes = {
            name: type(name, (Message,), {})
            for name in (
                "AssistantMessage", "StreamEvent", "SystemMessage", "ResultMessage",
                "UserMessage", "ToolResultBlock", "ToolUseBlock",
            )
        }
        events, summaries, queries = [], [], []

        class Client:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def query(self, prompt):
                queries.append(prompt)

            async def receive_response(self):
                yield classes["AssistantMessage"](content=[
                    classes["ToolUseBlock"](id="weather-1", name="weather", input={"location": "上海"})
                ])
                yield classes["UserMessage"](content=[
                    classes["ToolResultBlock"](tool_use_id="weather-1", content="上海，小雨，24℃", is_error=False)
                ])
                while True:
                    await asyncio.sleep(0.005)
                    yield classes["StreamEvent"](event={
                        "type": "content_block_delta",
                        "delta": {"type": "text_delta", "text": "正在整理"},
                    })

        async def summarize(self, prompt, evidence):
            summaries.append(evidence)
            return "上海现在有小雨，气温24℃。"

        async def emit(event):
            events.append(event["data"])

        with tempfile.TemporaryDirectory() as workdir, ExitStack() as stack:
            for name, cls in classes.items():
                stack.enter_context(patch.object(module, name, cls))
            stack.enter_context(patch.object(module, "ClaudeSDKClient", Client))
            stack.enter_context(patch.object(module, "ClaudeAgentOptions", lambda **kwargs: kwargs))
            stack.enter_context(patch.object(module, "_build_owui_mcp_server", return_value=(None, ["weather"], {"weather": "local_get_current_weather"})))
            stack.enter_context(patch.object(Pipe, "_summarize_completed_tools", summarize))
            pipe = Pipe()
            pipe.valves.WORKDIR_ROOT = workdir
            pipe.valves.TOOL_GOVERNANCE_ENABLED = False
            pipe.valves.FAST_READ_ONLY_FOLLOWUP_TIMEOUT_SECONDS = 0.03
            chunks = [chunk async for chunk in pipe.pipe(
                {"messages": [{"role": "user", "content": "查询上海天气"}]},
                __chat_id__="busy-weather-test",
                __tools__={"weather": {"callable": lambda: None, "spec": {}}},
                __event_emitter__=emit,
            )]

        self.assertEqual(len(queries), 1)
        self.assertEqual(summaries, ["上海，小雨，24℃"])
        self.assertIn("上海现在有小雨，气温24℃。", chunks)
        self.assertNotIn("正在整理", "".join(chunks))
        self.assertTrue(events[-1]["done"])

    async def test_model_silence_timeout_stops_waiting(self) -> None:
        async def silent_source():
            await asyncio.sleep(0.05)
            yield "late"

        with self.assertRaises(asyncio.TimeoutError):
            async for _ in _with_silence_timeout(silent_source(), 0.01):
                pass

    async def test_post_tool_deadline_is_not_reset_by_stream_events(self) -> None:
        async def busy_source():
            while True:
                await asyncio.sleep(0.008)
                yield "text_delta"

        seen = []
        deadline = time.monotonic() + 0.035
        with self.assertRaises(_ToolFollowupTimeout):
            async for event in _with_silence_timeout(
                busy_source(), 0.1, lambda: deadline
            ):
                seen.append(event)
        self.assertTrue(seen)

    async def test_human_confirmation_wait_does_not_expire_model_deadlines(self) -> None:
        waiting = False

        async def source():
            nonlocal waiting
            waiting = True
            try:
                await asyncio.sleep(0.5)
            finally:
                waiting = False
            yield "confirmed result"
            await asyncio.sleep(0.003)
            yield "model followup"

        deadline = time.monotonic() + 0.2
        seen = [item async for item in _with_silence_timeout(
            source(), 0.2, lambda: deadline, lambda: waiting,
        )]
        self.assertEqual(seen, ["confirmed result", "model followup"])

    async def test_silence_protection_still_applies_without_human_wait(self) -> None:
        async def source():
            await asyncio.sleep(0.06)
            yield "late"

        with self.assertRaises(asyncio.TimeoutError):
            async for _ in _with_silence_timeout(source(), 0.01, user_waiting=lambda: False):
                pass

    def test_fast_followup_only_matches_explicit_read_only_tools(self) -> None:
        configured = "get_current_weather,get_weather_forecast"
        self.assertTrue(_is_fast_read_only_tool("local-weather_get_current_weather", configured))
        self.assertTrue(_is_fast_read_only_tool("get_weather_forecast", configured))
        self.assertFalse(_is_fast_read_only_tool("create_jira_ticket", configured))
        self.assertFalse(_is_fast_read_only_tool("unsafe_get_current_weather", ""))

    async def test_progress_keeps_owui_updated_until_completion(self) -> None:
        events = []

        async def emit(event):
            events.append(event)

        progress = _LiveProgress(emit, interval=0.01)
        await progress.set("正在等待模型响应……")
        await asyncio.sleep(0.035)
        await progress.set("正在调用工具：echo_owui")
        await progress.finish("已完成。")

        descriptions = [event["data"]["description"] for event in events]
        self.assertIn("正在等待模型响应……", descriptions)
        self.assertTrue(any(item.startswith("⏳ ") for item in descriptions))
        self.assertIn("正在调用工具：echo_owui", descriptions)
        self.assertEqual(events[-1]["data"], {"description": "已完成。", "done": True})

    async def test_owui_mode_without_tools_skips_cli_agent(self) -> None:
        calls = []

        async def fake_messages_api(self, body, user, metadata, files, emitter):
            calls.append((body, metadata, files))
            yield "您好，有什么可以帮您？"

        with tempfile.TemporaryDirectory() as workdir:
            pipe = Pipe()
            pipe.valves.WORKDIR_ROOT = workdir
            pipe.valves.TOOL_MODE = "owui_only"
            with patch.object(Pipe, "_run_messages_api", fake_messages_api):
                chunks = [
                    chunk
                    async for chunk in pipe.pipe(
                        {"messages": [{"role": "user", "content": "请解释什么是 MCP"}]},
                        __chat_id__="plain-chat-test",
                        __metadata__={"session_id": "plain-chat-test"},
                        __tools__={},
                    )
                ]

        self.assertEqual(chunks, ["您好，有什么可以帮您？"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1], {"session_id": "plain-chat-test"})

    async def test_standalone_greeting_uses_plain_chat_even_with_tools(self) -> None:
        calls = []

        async def fake_messages_api(self, body, user, metadata, files, emitter):
            calls.append((metadata, files))
            yield "你好！"

        pipe = Pipe()
        with patch.object(Pipe, "_run_messages_api", fake_messages_api):
            chunks = [
                chunk
                async for chunk in pipe.pipe(
                    {"messages": [{"role": "user", "content": "你好"}]},
                    __chat_id__="greeting-test",
                    __metadata__={"session_id": "greeting-test"},
                    __tools__={"unused": {"type": "builtin"}},
                )
            ]

        self.assertEqual(chunks, ["你好！"])
        self.assertEqual(calls, [(None, None)])

    async def test_capability_question_lists_granted_tools_without_running_them(self) -> None:
        calls = []

        async def fake_messages_api(self, body, user, metadata, files, emitter):
            calls.append(body)
            yield "可以使用 echo_owui 工具。"

        with tempfile.TemporaryDirectory() as workdir:
            pipe = Pipe()
            pipe.valves.WORKDIR_ROOT = workdir
            with patch.object(Pipe, "_run_messages_api", fake_messages_api):
                chunks = [
                    chunk
                    async for chunk in pipe.pipe(
                        {"messages": [{"role": "user", "content": "你可以做啥啊"}]},
                        __chat_id__="capability-test",
                        __tools__={"echo_owui": {"type": "custom"}},
                    )
                ]

        self.assertEqual(chunks, ["可以使用 echo_owui 工具。"])
        self.assertEqual(len(calls), 1)
        self.assertIn("echo_owui", calls[0]["messages"][0]["content"])


if __name__ == "__main__":
    unittest.main()
