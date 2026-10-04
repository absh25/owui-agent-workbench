import json
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock, patch

from supervisor.native_pipe import Pipe, convert_messages
from supervisor.plan_results import current_plan_result, can_correct_plan, summarize_plan
from supervisor.artifacts import current_artifacts


def history(report):
    return [{'role': 'user', 'content': '执行并行计划'},
            {'role': 'assistant', 'tool_calls': [{'id': 'plan1', 'function': {
                'name': 'execute_task_plan', 'arguments': '{}'}}]},
            {'role': 'tool', 'tool_call_id': 'plan1', 'content': json.dumps(report)}]


def api():
    client = AsyncMock()
    client.is_closed = Mock(return_value=False)
    client.messages.create.return_value = NS(content=[NS(type='text', text='可以执行计划')])
    return client


class PlanIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_installs_callable_in_actual_native_map_and_uses_only_authorized_tools(self):
        execute = AsyncMock(return_value={'projects': []})
        name = 'company-jira_get_jira_create_context'
        tools = {name: {'spec': {'name': name, 'parameters': {'type': 'object',
            'properties': {'project_key': {'type': 'string'}}}},
            'callable': execute, 'supervisor_governed': True}}
        client = api()
        with patch('supervisor.native_pipe.AsyncAnthropic', return_value=client):
            await Pipe().pipe({'stream': False, 'messages': [{'role': 'user', 'content': '并行查询'}]},
                              __tools__=tools, __user__={'id': 'alice'})
        self.assertIn('execute_task_plan', tools)
        self.assertIn('execute_task_plan', [d['name'] for d in client.messages.create.call_args.kwargs['tools']])
        report = await tools['execute_task_plan']['callable'](tasks=[{'id': 'B', 'title': '项目',
            'tool': name, 'arguments': {'project_key': ''}, 'depends_on': []}])
        self.assertEqual(report['status'], 'completed')
        execute.assert_awaited_once_with(project_key='')
        denied = await tools['execute_task_plan']['callable'](tasks=[{'id': 'B', 'title': '他人工具',
            'tool': 'create_jira_ticket', 'arguments': {}, 'depends_on': []}])
        self.assertEqual(denied['status'], 'invalid_arguments')
        self.assertEqual(execute.await_count, 1)

    async def test_no_identity_or_internal_task_cannot_receive_plan(self):
        for user, task in [(None, None), ({'id': 'alice'}, 'title_generation')]:
            tools = {'get_current_weather': {'spec': {'parameters': {'type': 'object', 'properties': {}}},
                        'callable': AsyncMock(), 'supervisor_governed': True}}
            with patch('supervisor.native_pipe.AsyncAnthropic', return_value=api()):
                await Pipe().pipe({'stream': False, 'messages': [{'role': 'user', 'content': '查询'}]},
                                  __tools__=tools, __user__=user, __task__=task)
            self.assertNotIn('execute_task_plan', tools)

    async def test_terminal_plan_report_short_circuits_model_and_does_not_retry_failure(self):
        report = {'status': 'partial_failed', 'execution_started': True, 'peak_parallel': 2,
                  'wall_seconds': 4.0, 'tasks': [{'id': 'A', 'title': '查证',
                      'tool': 'delegate_retrieval_task', 'depends_on': [], 'status': 'failed',
                      'started_after_seconds': 0.0, 'wall_seconds': 2,
                      'result': {'status': 'insufficient_evidence', 'summary': '资料不足',
                                 'missing_information': ['没有通用性能保证']}},
                      {'id': 'C', 'title': '编码', 'tool': 'delegate_coding_task',
                       'depends_on': ['A'], 'status': 'blocked', 'reason': '前置失败'}]}
        with patch('supervisor.native_pipe.AsyncAnthropic') as constructor:
            response = await Pipe().pipe({'stream': False, 'messages': history(report)}, __user__={'id': 'alice'})
        constructor.assert_not_called()
        text = response['choices'][0]['message']['content']
        self.assertIn('未全部完成', text)
        self.assertIn('依赖阻断', text)
        self.assertIn('没有通用性能保证', text)

    def test_correctable_invalid_plan_is_bounded_and_execution_failure_cannot_be_hidden(self):
        invalid = {'status': 'invalid_arguments', 'execution_started': False}
        messages = history(invalid)
        self.assertTrue(can_correct_plan(current_plan_result(messages)))
        for i in range(2):
            messages.extend(history(invalid)[1:])
        self.assertFalse(can_correct_plan(current_plan_result(messages)))
        failed = {'status': 'partial_failed', 'execution_started': True}
        messages = history(failed) + history(invalid)[1:]
        self.assertEqual(current_plan_result(messages), failed)
        self.assertIsNone(current_plan_result(messages + [{'role': 'user', 'content': '下一件任务'}]))

    async def test_plan_cannot_be_batched_with_extra_tool(self):
        client = api()
        client.messages.create.return_value = NS(content=[
            NS(type='tool_use', name='execute_task_plan', id='a', input={'tasks': []}),
            NS(type='tool_use', name='get_current_weather', id='b', input={})])
        fn = {'name': 'get_current_weather', 'parameters': {'type': 'object', 'properties': {}}}
        tools = {fn['name']: {'spec': fn, 'callable': AsyncMock(), 'supervisor_governed': True}}
        with patch('supervisor.native_pipe.AsyncAnthropic', return_value=client):
            response = await Pipe().pipe({'stream': False, 'messages': [{'role': 'user', 'content': '并行'}],
                'tools': [{'type': 'function', 'function': fn}]}, __tools__=tools, __user__={'id': 'alice'})
        self.assertNotIn('tool_calls', response['choices'][0]['message'])
        self.assertIn('本批未执行', response['choices'][0]['message']['content'])

    def test_successful_coding_context_is_compacted_without_modifying_original_trace(self):
        report = {'status': 'completed', 'tool_evidence': [{'output': 'x' * 50000}],
                  'verification': {'passed': True, 'checks': [{'command': 'python -m unittest',
                      'exit_code': 0, 'output': 'Ran 3 tests\nOK'}], 'history': [{'details': 'x' * 50000}]}}
        raw = json.dumps(report)
        messages = [{'role': 'assistant', 'tool_calls': [{'id': 'c', 'function': {
            'name': 'delegate_coding_task', 'arguments': '{}'}}]},
            {'role': 'tool', 'tool_call_id': 'c', 'content': raw}]
        converted = convert_messages(messages)[1][-1]['content'][0]['content']
        compact = json.loads(converted)
        self.assertTrue(compact['verification']['passed'])
        self.assertEqual(compact['verification']['checks'][0]['exit_code'], 0)
        self.assertNotIn('tool_evidence', compact)
        self.assertEqual(messages[-1]['content'], raw)
        report['status'] = 'verification_failed'
        messages[-1]['content'] = json.dumps(report)
        self.assertEqual(convert_messages(messages)[1][-1]['content'][0]['content'], messages[-1]['content'])

    def test_nested_plan_artifacts_require_verified_successful_coding_node(self):
        artifact = {'file_id': 'verified-image'}
        good = {'id': 'C', 'tool': 'coding_agent_delegate_coding_task', 'status': 'succeeded',
                'result': {'status': 'completed', 'artifacts': [artifact], 'verification': {'passed': True}}}
        self.assertEqual(current_artifacts(history({'tasks': [good]})), [artifact])
        good['result']['verification']['passed'] = False
        self.assertEqual(current_artifacts(history({'tasks': [good]})), [])

    def test_summary_uses_recorded_tests_and_never_sdk_success_alone(self):
        task = {'id': 'C', 'title': '编程', 'tool': 'delegate_coding_task', 'depends_on': [],
                'status': 'failed', 'result': {'status': 'completed', 'verification': {'passed': False,
                    'checks': [{'command': 'python -m unittest', 'exit_code': 1,
                                'output': 'Ran 3 tests\nFAILED'}]}}}
        text = summarize_plan({'status': 'partial_failed', 'tasks': [task]})
        self.assertIn('未通过', text)
        self.assertIn('实际 3 个测试方法', text)
        self.assertIn('退出码 1', text)

    def test_busy_is_not_an_acceptance_failure_and_cosmetic_suggestions_do_not_block(self):
        task = {'id': 'C', 'title': '编程', 'tool': 'delegate_coding_task', 'depends_on': [],
                'status': 'failed', 'result': {'status': 'workspace_busy', 'verification': {'passed': False}}}
        text = summarize_plan({'status': 'partial_failed', 'tasks': [task]})
        self.assertIn('尚未运行（工作区忙', text)
        task.update(status='succeeded', result={'status': 'completed', 'verification': {
            'passed': True, 'suggestions': ['可以调整配色']}})
        text = summarize_plan({'status': 'completed', 'tasks': [task]})
        self.assertIn('独立验收：通过', text)
        self.assertIn('改进建议（不阻塞验收）：可以调整配色', text)
