import json
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock, patch

from supervisor.native_pipe import Pipe, current_prepared_coding_result, PELICAN_DEMO_PROMPT


def fake_api(text="```python\nprint('hello')\n```"):
    client = AsyncMock()
    client.is_closed = Mock(return_value=False)
    client.messages.create.return_value = NS(content=[NS(type="text", text=text)])
    return client


def entry(name, tool_id, *, demo=False):
    properties = {"task": {"type": "string"}}
    if demo:
        properties["demo_scene"] = {"type": "string"}
    spec = {"name": name, "parameters": {"type": "object", "properties": properties}}
    return {"spec": spec, "tool_id": tool_id, "callable": AsyncMock(), "supervisor_governed": True}


def body_for(entries, **values):
    return {"stream": False, "messages": [{"role": "user", "content": "写一个 Python 程序"}],
            "tools": [{"type": "function", "function": tool["spec"]} for tool in entries.values()], **values}


def prepared_history(result, *, call_id="coding-1", result_id="coding-1", scene="pelican_bike"):
    return [{"role": "user", "content": "鹈鹕骑车动画"},
            {"role": "assistant", "tool_calls": [{"id": call_id, "function": {
                "name": "delegate_coding_task", "arguments": json.dumps({"demo_scene": scene})}}]},
            {"role": "tool", "tool_call_id": result_id, "content": json.dumps(result)}]


def verified_fixture():
    return {"status": "completed", "prepared": True, "execution_mode": "demo_fixture",
            "scene": "pelican_bike", "sdk_used": False, "verification": {"passed": True},
            "changed_files": ["pelican-riding-bike.html"], "artifacts": [{"file_id": "verified-html"}]}


class CodingSelectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_prepared_command_produces_real_native_call_without_model_round(self):
        tools = {'delegate_coding_task': entry('delegate_coding_task', 'coding_agent', demo=True)}
        pipe = Pipe()
        pipe.valves.ENABLE_PELICAN_DEMO = True
        body = body_for(tools)
        body['messages'][0]['content'] = PELICAN_DEMO_PROMPT
        with patch('supervisor.native_pipe.AsyncAnthropic') as model:
            result = await pipe.pipe(body, __tools__=tools, __user__={'id': 'owner'}, __metadata__={'tool_ids': ['coding_agent']})
        model.assert_not_called()
        call = result['choices'][0]['message']['tool_calls'][0]
        self.assertEqual(result['choices'][0]['finish_reason'], 'tool_calls')
        self.assertEqual(json.loads(call['function']['arguments'])['task'], '交付预制鹈鹕骑车动画')
        self.assertEqual(json.loads(call['function']['arguments'])['demo_scene'], 'pelican_bike')
        tools['delegate_coding_task']['callable'].assert_not_awaited()

    async def test_exact_command_cannot_override_closed_tool_or_added_requirements(self):
        for selected, suffix in ((False, ''), (True, '另外请改成夜晚')):
            tools = {'delegate_coding_task': entry('delegate_coding_task', 'coding_agent', demo=True)}
            pipe = Pipe()
            pipe.valves.ENABLE_PELICAN_DEMO = True
            body = body_for(tools)
            body['messages'][0]['content'] = PELICAN_DEMO_PROMPT + suffix
            model = fake_api()
            with patch('supervisor.native_pipe.AsyncAnthropic', return_value=model):
                await pipe.pipe(body, __tools__=tools, __user__={'id': 'owner'}, __metadata__={'tool_ids': ['coding_agent'] if selected else []})
            model.messages.create.assert_awaited_once()

    async def test_disabled_coding_returns_current_model_code_without_delegation(self):
        tools = {"delegate_coding_task": entry("delegate_coding_task", "coding_agent"),
                 "delegate_retrieval_task": entry("delegate_retrieval_task", "retrieval_agent")}
        body = body_for(tools)
        coding = tools["delegate_coding_task"]["callable"]
        api = fake_api()
        with patch("supervisor.native_pipe.AsyncAnthropic", return_value=api):
            result = await Pipe().pipe(body, __tools__=tools, __user__={"id": "owner"},
                                       __metadata__={"tool_ids": ["retrieval_agent"]})
        self.assertEqual(result["choices"][0]["message"]["content"], "```python\nprint('hello')\n```")
        self.assertNotIn("delegate_coding_task", tools)
        self.assertIn("编码工具未开启或未获授权", api.messages.create.call_args.kwargs["system"])
        coding.assert_not_awaited()
        plan_spec = tools["execute_task_plan"]["spec"]
        allowed = plan_spec["parameters"]["properties"]["tasks"]["items"]["properties"]["tool"]["enum"]
        self.assertEqual(allowed, ["delegate_retrieval_task"])
        rejected = await tools["execute_task_plan"]["callable"](tasks=[{
            "id": "code", "title": "禁止绕开关闭开关", "tool": "delegate_coding_task", "arguments": {}, "depends_on": []}])
        self.assertEqual(rejected["status"], "invalid_arguments")
        self.assertFalse(rejected["execution_started"])
        coding.assert_not_awaited()

    async def test_explicit_empty_tools_disables_even_stale_execution_entries(self):
        tools = {"delegate_coding_task": entry("delegate_coding_task", "coding_agent")}
        coding = tools["delegate_coding_task"]["callable"]
        api = fake_api()
        with patch("supervisor.native_pipe.AsyncAnthropic", return_value=api):
            result = await Pipe().pipe(body_for(tools, tools=[]), __tools__=tools, __user__={"id": "owner"})
        self.assertEqual(tools, {})
        self.assertNotIn("tools", api.messages.create.call_args.kwargs)
        self.assertIn("print('hello')", result["choices"][0]["message"]["content"])
        coding.assert_not_awaited()

    async def test_disabled_tool_call_is_rejected_instead_of_executed(self):
        tools = {"delegate_coding_task": entry("delegate_coding_task", "coding_agent")}
        coding = tools["delegate_coding_task"]["callable"]
        api = fake_api()
        api.messages.create.return_value = NS(content=[NS(type="tool_use", id="bad", name="delegate_coding_task", input={})])
        with patch("supervisor.native_pipe.AsyncAnthropic", return_value=api):
            result = await Pipe().pipe(body_for(tools), __tools__=tools, __user__={"id": "owner"},
                                       __metadata__={"tool_ids": []})
        self.assertNotIn("tool_calls", result["choices"][0]["message"])
        self.assertIn("未执行", result["choices"][0]["message"]["content"])
        coding.assert_not_awaited()

    async def test_enabled_tool_preserves_normal_native_delegation(self):
        tools = {"delegate_coding_task": entry("delegate_coding_task", "coding_agent")}
        api = fake_api()
        api.messages.create.return_value = NS(content=[NS(type="tool_use", id="code", name="delegate_coding_task", input={"task": "修复"})])
        with patch("supervisor.native_pipe.AsyncAnthropic", return_value=api):
            result = await Pipe().pipe(body_for(tools), __tools__=tools, __user__={"id": "owner"},
                                       __metadata__={"tool_ids": ["coding_agent"]})
        self.assertEqual(result["choices"][0]["finish_reason"], "tool_calls")
        self.assertEqual(result["choices"][0]["message"]["tool_calls"][0]["function"]["name"], "delegate_coding_task")
        self.assertIn("编码后端由当前工具配置决定", api.messages.create.call_args.kwargs["system"])
        tools["delegate_coding_task"]["callable"].assert_not_awaited()

    async def test_schema_hidden_tool_cannot_be_reintroduced_by_plan(self):
        tools = {"delegate_coding_task": entry("delegate_coding_task", "coding_agent"),
                 "delegate_retrieval_task": entry("delegate_retrieval_task", "retrieval_agent")}
        body = body_for(tools)
        body["tools"] = [body["tools"][1]]
        api = fake_api()
        with patch("supervisor.native_pipe.AsyncAnthropic", return_value=api):
            await Pipe().pipe(body, __tools__=tools, __user__={"id": "owner"})
        self.assertNotIn("delegate_coding_task", tools)
        schema = tools["execute_task_plan"]["spec"]["parameters"]["properties"]["tasks"]["items"]["properties"]["tool"]
        self.assertEqual(schema["enum"], ["delegate_retrieval_task"])

    async def test_demo_hint_requires_both_pipe_opt_in_and_selected_supported_coding_tool(self):
        for opted_in, selected, supported in ((False, True, True), (True, False, True), (True, True, False), (True, True, True)):
            with self.subTest(opted_in=opted_in, selected=selected, supported=supported):
                tools = {"delegate_coding_task": entry("delegate_coding_task", "coding_agent", demo=supported)}
                api = fake_api()
                pipe = Pipe()
                pipe.valves.ENABLE_PELICAN_DEMO = opted_in
                with patch("supervisor.native_pipe.AsyncAnthropic", return_value=api):
                    await pipe.pipe(body_for(tools), __tools__=tools, __user__={"id": "owner"},
                                    __metadata__={"tool_ids": ["coding_agent"] if selected else []})
                system = api.messages.create.call_args.kwargs["system"]
                self.assertEqual("demo_scene='pelican_bike'" in system, opted_in and selected and supported)

    async def test_verified_prepared_result_skips_second_model_call_and_preserves_artifact_preview(self):
        messages = prepared_history(verified_fixture())
        preview = AsyncMock(return_value="\n\nACTUAL_ARTIFACT_PREVIEW")
        with patch("supervisor.native_pipe.AsyncAnthropic") as api, patch("supervisor.native_pipe.previews", preview):
            result = await Pipe().pipe({"stream": False, "messages": messages}, __user__={"id": "owner"})
        api.assert_not_called()
        preview.assert_awaited_once_with(messages, "owner")
        text = result["choices"][0]["message"]["content"]
        self.assertIn("预制演示模板", text)
        self.assertIn("sdk_used=false", text)
        self.assertIn("ACTUAL_ARTIFACT_PREVIEW", text)

    async def test_verified_prepared_stream_keeps_preview_and_has_no_extra_model_round(self):
        with patch("supervisor.native_pipe.AsyncAnthropic") as api, \
                patch("supervisor.native_pipe.previews", AsyncMock(return_value="REAL_PREVIEW")):
            response = await Pipe().pipe({"stream": True, "messages": prepared_history(verified_fixture())}, __user__={"id": "owner"})
            stream = "".join([item async for item in response.body_iterator])
        api.assert_not_called()
        self.assertIn("REAL_PREVIEW", stream)
        self.assertIn("sdk_used=false", stream)
        self.assertIn("[DONE]", stream)

    def test_fixture_shortcut_requires_exact_mode_scene_sdk_verification_and_call_binding(self):
        for field, value in (("execution_mode", "sdk"), ("scene", "cat"), ("sdk_used", True),
                             ("sdk_used", None), ("status", "verification_failed"), ("verification", {"passed": False})):
            with self.subTest(field=field, value=value):
                report = verified_fixture()
                report[field] = value
                self.assertIsNone(current_prepared_coding_result(prepared_history(report)))
        self.assertIsNone(current_prepared_coding_result(prepared_history(verified_fixture(), result_id="unrelated")))
        self.assertIsNone(current_prepared_coding_result(prepared_history(verified_fixture(), scene="")))
        self.assertIsNone(current_prepared_coding_result(prepared_history(verified_fixture()) + [{"role": "user", "content": "新任务"}]))

    async def test_normal_sdk_success_keeps_existing_model_followup(self):
        report = verified_fixture()
        report.update(execution_mode="sdk", sdk_used=True)
        api = fake_api("正常编码结果")
        with patch("supervisor.native_pipe.AsyncAnthropic", return_value=api):
            result = await Pipe().pipe({"stream": False, "messages": prepared_history(report)}, __user__={"id": "owner"})
        api.messages.create.assert_awaited_once()
        self.assertEqual(result["choices"][0]["message"]["content"], "正常编码结果")


if __name__ == "__main__":
    unittest.main()
