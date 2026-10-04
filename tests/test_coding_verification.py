import asyncio
import json
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch

from supervisor.coding_tool import Tools, invalid_arguments, structured_result, validate_arguments
from supervisor.verification import AcceptanceVerifier, check_commands, run_verified_session, terminal_command
from supervisor.native_pipe import Pipe, current_coding_result, coding_failure_notice, can_correct_coding_arguments


def inventory(**files):
    return {'files': files, 'scripts': {}}


class FakeResult:
    def __init__(self, result='{}', subtype='success', is_error=False):
        self.result, self.subtype, self.is_error = result, subtype, is_error
        self.session_id = 'same-session'


class FakeClient:
    def __init__(self, results):
        self.results = iter(results)
        self.prompts = []

    async def query(self, prompt):
        self.prompts.append(prompt if isinstance(prompt, str) else [item async for item in prompt])

    async def receive_response(self):
        yield next(self.results)


class CodingVerificationTests(unittest.IsolatedAsyncioTestCase):
    def verifier(self, initial=None, **kwargs):
        return AcceptanceVerifier({}, '/workspace/owner', lambda value: value,
            initial or inventory(**{'app.py': 'old'}), kwargs.pop('commands', []),
            '修复程序', '实际要求', **kwargs)

    def test_sdk_success_and_claimed_tests_cannot_bypass_acceptance(self):
        result = FakeResult(json.dumps({'summary': '全部成功', 'tests': ['passed']}))
        for verification in (None, {'passed': False, 'issues': ['测试失败']}, {'passed': 'true'}):
            with self.subTest(verification=verification):
                report = structured_result(result, 'task', '/workspace/owner', [], verification)
                self.assertEqual(report['status'], 'verification_failed')
                self.assertEqual(report['tests']['status'], 'not_run')
        verified = {'passed': True, 'checks': [{'exit_code': 0}], 'changed_files': ['app.py']}
        self.assertEqual(structured_result(result, 'task', '/workspace/owner', [], verified)['status'], 'completed')
        self.assertEqual(structured_result(FakeResult(subtype='error_max_turns'), 'task', '/workspace/owner', [], verified)['status'], 'failed')

    def test_acceptance_commands_are_frozen_and_failure_cannot_be_swallowed(self):
        commands = ['python -m unittest -v']
        fixed = check_commands(commands, inventory())
        commands.clear()
        self.assertEqual(fixed, ('python -m unittest -v',))
        for command in ('echo passed', 'npm test || true', 'pytest; true', 'pytest | cat',
                        'python -c "print(1)"', 'npm test > result.txt'):
            with self.assertRaises(ValueError):
                check_commands([command], inventory())

    async def test_changed_code_without_tests_fails_and_read_only_edits_fail(self):
        after = inventory(**{'app.py': 'new'})
        for read_only in (False, True):
            with patch('supervisor.verification.snapshot', AsyncMock(return_value=after)):
                result = await self.verifier(read_only=read_only).verify({})
            self.assertFalse(result['passed'])
            self.assertEqual(result['changed_files'], ['app.py'])

    async def test_unchanged_read_only_analysis_needs_no_test(self):
        initial = inventory(**{'app.py': 'old'})
        with patch('supervisor.verification.snapshot', AsyncMock(return_value=initial)):
            result = await self.verifier(read_only=True).verify({})
        self.assertTrue(result['passed'])
        self.assertEqual(result['checks'], [])

    async def test_real_failure_and_empty_test_suite_block_completion(self):
        after = inventory(**{'app.py': 'new'})
        for code, output in ((1, 'AssertionError'), (0, 'Ran 0 tests\nOK')):
            with patch('supervisor.verification.snapshot', AsyncMock(return_value=after)), \
                 patch('supervisor.verification.terminal_command', AsyncMock(return_value={
                     'command': 'python -m unittest -v', 'exit_code': code, 'output': output})):
                result = await self.verifier(commands=['python -m unittest -v']).verify({})
            self.assertFalse(result['passed'])

    async def test_files_modified_after_checks_are_not_delivered(self):
        with patch('supervisor.verification.snapshot', AsyncMock(side_effect=[
                inventory(**{'app.py': 'new'}), inventory(**{'app.py': 'newer'})])), \
             patch('supervisor.verification.terminal_command', AsyncMock(return_value={
                 'exit_code': 0, 'output': 'Ran 2 tests\nOK'})):
            result = await self.verifier(commands=['python -m unittest -v']).verify({})
        self.assertFalse(result['passed'])
        self.assertTrue(any('验收期间' in issue for issue in result['issues']))

    async def test_successful_render_still_requires_visual_acceptance(self):
        after = inventory(**{'scene.html': 'new'})
        browser = {'passed': True, 'issues': [], 'source_sha256': 'new',
                   'screenshots': [{'name': 'desktop.png', 'data': 'image'}]}
        for passed in (False, True):
            review = {'passed': passed, 'issues': [] if passed else ['角色悬空'], 'checked': ['骑车姿态']}
            with patch('supervisor.verification.snapshot', AsyncMock(return_value=after)), \
                 patch('supervisor.verification.render_target', AsyncMock(return_value=dict(browser))), \
                 patch('supervisor.verification.review_screenshots', AsyncMock(return_value=review)) as check:
                result = await self.verifier(initial=inventory(), visual_model='vision-model').verify({})
            self.assertEqual(result['passed'], passed)
            check.assert_awaited_once()
            self.assertTrue(result['browser'])

    async def test_browser_unavailable_fails_closed_and_ui_components_need_preview(self):
        after = inventory(**{'scene.html': 'new'})
        with patch('supervisor.verification.snapshot', AsyncMock(return_value=after)), \
             patch('supervisor.verification.render_target', AsyncMock(side_effect=RuntimeError('browser unavailable'))):
            result = await self.verifier(initial=inventory()).verify({})
        self.assertFalse(result['passed'])
        after = inventory(**{'App.tsx': 'new'})
        with patch('supervisor.verification.snapshot', AsyncMock(return_value=after)):
            result = await self.verifier(initial=inventory()).verify({})
        self.assertFalse(result['passed'])
        self.assertTrue(any('preview_target' in issue for issue in result['issues']))

    async def test_failed_acceptance_continues_same_session_then_passes(self):
        client = FakeClient([FakeResult(), FakeResult()])
        verifier = NS(images=[], verify=AsyncMock(side_effect=[
            {'passed': False, 'issues': ['AssertionError'], 'checks': [{'exit_code': 1}]},
            {'passed': True, 'issues': [], 'checks': [{'exit_code': 0}]}]))
        result, verification = await run_verified_session(client, 'task', verifier, FakeResult, 2, AsyncMock())
        self.assertTrue(verification['passed'])
        self.assertEqual(verification['attempts'], 2)
        self.assertEqual(len(client.prompts), 2)
        self.assertIn('AssertionError', client.prompts[1])

    async def test_visual_failure_sends_actual_images_and_stops_at_repair_limit(self):
        client = FakeClient([FakeResult(), FakeResult()])
        verifier = NS(images=[{'name': 'desktop.png', 'data': 'actual-png'}], verify=AsyncMock(
            return_value={'passed': False, 'issues': ['悬空'], 'checks': []}))
        result, verification = await run_verified_session(client, 'task', verifier, FakeResult, 1, AsyncMock())
        self.assertFalse(verification['passed'])
        self.assertEqual(len(client.prompts), 2)
        message = client.prompts[1][0]
        self.assertEqual(message['session_id'], 'same-session')
        self.assertEqual(message['message']['content'][-1]['source']['data'], 'actual-png')

    async def test_sdk_turn_limit_is_not_bypassed_by_a_repair_query(self):
        client = FakeClient([FakeResult(subtype='error_max_turns', is_error=True)])
        verifier = NS(images=[], verify=AsyncMock())
        result, verification = await run_verified_session(client, 'task', verifier, FakeResult, 2, AsyncMock())
        self.assertEqual(len(client.prompts), 1)
        verifier.verify.assert_not_awaited()

    def test_failed_current_turn_has_notice_and_old_or_unrelated_results_do_not(self):
        messages = [{'role': 'user', 'content': '修复'}, {'role': 'assistant', 'tool_calls': [
            {'id': 'code-1', 'function': {'name': 'delegate_coding_task'}}]},
            {'role': 'tool', 'tool_call_id': 'code-1', 'content': json.dumps({
                'status': 'verification_failed', 'verification': {'passed': False, 'issues': ['测试失败']}})}]
        self.assertIn('未完成交付', coding_failure_notice(current_coding_result(messages)))
        self.assertIsNone(current_coding_result(messages + [{'role': 'user', 'content': '另一个任务'}]))
        messages[-1]['tool_call_id'] = 'other'
        self.assertIsNone(current_coding_result(messages))

    async def test_preserved_tests_cannot_be_weakened_to_make_checks_green(self):
        initial = inventory(**{'app.py': 'old', 'test_app.py': 'original'})
        verifier = AcceptanceVerifier({}, '/workspace/owner', lambda value: value, initial,
            ['python -m unittest -v'], '修复程序，不要修改测试。', '')
        after = inventory(**{'app.py': 'new', 'test_app.py': 'weakened'})
        with patch('supervisor.verification.snapshot', AsyncMock(return_value=after)), \
             patch('supervisor.verification.terminal_command', AsyncMock(return_value={
                 'exit_code': 0, 'output': 'Ran 2 tests\nOK'})):
            result = await verifier.verify({})
        self.assertFalse(result['passed'])
        self.assertTrue(any('原有测试' in issue for issue in result['issues']))

    async def test_main_cannot_redelegate_failed_coding_in_same_turn_and_includes_failure_notice(self):
        api = AsyncMock()
        api.is_closed = lambda: False
        api.messages.create.return_value = NS(content=[NS(type='text', text='剩余错误需要处理')])
        tools = {'delegate_coding_task': {'supervisor_governed': True}}
        body = {'stream': False, 'messages': [
            {'role': 'user', 'content': '修复'},
            {'role': 'assistant', 'tool_calls': [{'id': 'code-1', 'function': {'name': 'delegate_coding_task', 'arguments': '{}'}}]},
            {'role': 'tool', 'tool_call_id': 'code-1', 'content': json.dumps({
                'status': 'verification_failed', 'verification': {'passed': False, 'issues': ['测试失败']}})}],
            'tools': [{'type': 'function', 'function': {'name': 'delegate_coding_task',
                       'parameters': {'type': 'object', 'properties': {}}}}]}
        with patch('supervisor.native_pipe.AsyncAnthropic', return_value=api):
            result = await Pipe().pipe(body, __tools__=tools)
        self.assertFalse(tools)
        api.messages.create.assert_not_awaited()
        self.assertIn('未完成交付', result['choices'][0]['message']['content'])

    async def test_invalid_parameters_are_retryable_before_terminal_or_sdk_execution(self):
        tool = Tools()
        for command in ('test -s /home/user/cat_doodle.svg && echo OK', 'ls -l /home/user/cat_doodle.svg'):
            with patch('supervisor.coding_tool.snapshot', AsyncMock()) as take_snapshot, \
                 patch('supervisor.coding_tool.ClaudeSDKClient') as sdk:
                result = await tool.delegate_coding_task('画猫', verification_commands=[command],
                                                         preview_target='cat_doodle.svg')
            self.assertEqual(result['status'], 'invalid_arguments')
            self.assertFalse(result['execution_started'])
            self.assertIn('verification_commands=[]', result['correction'])
            take_snapshot.assert_not_awaited()
            sdk.assert_not_called()
        validate_arguments([], 'cat_doodle.svg', '.')
        with self.assertRaises(ValueError):
            validate_arguments([], '/home/user/cat_doodle.svg', '.')

    async def test_argument_correction_keeps_delegate_tool_and_budget_is_separate(self):
        api = AsyncMock()
        api.is_closed = lambda: False
        api.messages.create.return_value = NS(content=[NS(type='tool_use', id='retry',
            name='delegate_coding_task', input={'task': '画猫', 'verification_commands': [], 'preview_target': 'cat.svg'})])
        error = invalid_arguments('不能用 ls 验收')
        messages = [{'role': 'user', 'content': '画猫'}, {'role': 'assistant', 'tool_calls': [
            {'id': 'code-1', 'function': {'name': 'delegate_coding_task', 'arguments': '{}'}}]},
            {'role': 'tool', 'tool_call_id': 'code-1', 'content': json.dumps(error)}]
        tools = {'delegate_coding_task': {'supervisor_governed': True}}
        body = {'stream': False, 'messages': messages, 'tools': [{'type': 'function', 'function': {
            'name': 'delegate_coding_task', 'parameters': {'type': 'object', 'properties': {}}}}]}
        with patch('supervisor.native_pipe.AsyncAnthropic', return_value=api):
            result = await Pipe().pipe(body, __tools__=tools)
        self.assertIn('delegate_coding_task', tools)
        self.assertEqual(result['choices'][0]['finish_reason'], 'tool_calls')
        self.assertTrue(can_correct_coding_arguments(current_coding_result(messages)))
        for index in range(2, 4):
            messages += [{'role': 'assistant', 'tool_calls': [{'id': f'code-{index}', 'function': {
                'name': 'delegate_coding_task'}}]}, {'role': 'tool', 'tool_call_id': f'code-{index}', 'content': json.dumps(error)}]
        self.assertFalse(can_correct_coding_arguments(current_coding_result(messages)))

    async def test_unknown_tool_batch_is_never_forwarded_in_either_mode(self):
        for streaming in (False, True):
            api = AsyncMock()
            api.is_closed = lambda: False
            valid = NS(type='tool_use', id='valid', name='delegate_coding_task', input={})
            invalid = NS(type='tool_use', id='invalid', name='run_command', input={})
            api.messages.create.return_value = NS(content=[valid, invalid])
            async def events():
                for index, block in enumerate((valid, invalid)):
                    yield NS(type='content_block_start', index=index, content_block=block)
                    yield NS(type='content_block_delta', index=index, delta=NS(type='input_json_delta', partial_json='{}'))
                yield NS(type='message_delta', delta=NS(stop_reason='tool_use'))
            context = AsyncMock()
            context.__aenter__.return_value = events()
            api.messages.stream = lambda **kwargs: context
            body = {'stream': streaming, 'messages': [{'role': 'user', 'content': '画猫'}], 'tools': [
                {'type': 'function', 'function': {'name': 'delegate_coding_task',
                 'parameters': {'type': 'object', 'properties': {}}}}]}
            tools = {'delegate_coding_task': {'supervisor_governed': True}, 'run_command': {'supervisor_governed': True}}
            with patch('supervisor.native_pipe.AsyncAnthropic', return_value=api):
                result = await Pipe().pipe(body, __tools__=tools)
                output = ''.join([item async for item in result.body_iterator]) if streaming else json.dumps(result, ensure_ascii=False)
            self.assertNotIn('tool_calls', output)
            self.assertIn('未执行', output)
            self.assertNotIn('run_command', tools)

    async def test_svg_preview_does_not_run_unrelated_workspace_test_suites(self):
        initial = inventory(**{'old/tests/test_old.py': 'original'})
        verifier = self.verifier(initial=initial, preview_target='cat.svg')
        self.assertEqual(verifier.commands, ())

    async def test_stream_only_publishes_complete_json_tool_batch(self):
        for arguments, reason, expected in (('{"task":"画猫"}', 'tool_use', True),
                                            ('{"task":', 'max_tokens', False),
                                            ('[]', 'tool_use', False)):
            api = AsyncMock()
            api.is_closed = lambda: False
            async def events():
                yield NS(type='content_block_start', index=0, content_block=NS(type='tool_use', id='code', name='delegate_coding_task', input={}))
                yield NS(type='content_block_delta', index=0, delta=NS(type='input_json_delta', partial_json=arguments))
                yield NS(type='message_delta', delta=NS(stop_reason=reason))
            context = AsyncMock()
            context.__aenter__.return_value = events()
            api.messages.stream = lambda **kwargs: context
            body = {'stream': True, 'messages': [{'role': 'user', 'content': '画猫'}], 'tools': [
                {'type': 'function', 'function': {'name': 'delegate_coding_task',
                 'parameters': {'type': 'object', 'properties': {}}}}]}
            with patch('supervisor.native_pipe.AsyncAnthropic', return_value=api):
                response = await Pipe().pipe(body, __tools__={'delegate_coding_task': {'supervisor_governed': True}})
                output = ''.join([item async for item in response.body_iterator])
            self.assertEqual('tool_calls' in output, expected)

    def test_parameter_error_cannot_unlock_a_previous_real_execution_failure(self):
        messages = [{'role': 'user', 'content': '画猫'}]
        for index, report in enumerate(({'status': 'verification_failed', 'verification': {'passed': False}},
                                       invalid_arguments('命令无效'))):
            messages += [{'role': 'assistant', 'tool_calls': [{'id': str(index), 'function': {'name': 'delegate_coding_task'}}]},
                         {'role': 'tool', 'tool_call_id': str(index), 'content': json.dumps(report)}]
        self.assertEqual(current_coding_result(messages)['status'], 'verification_failed')
        self.assertFalse(can_correct_coding_arguments(current_coding_result(messages)))

    async def test_background_check_waits_for_real_exit_code(self):
        run = AsyncMock(return_value={'id': 'process-1', 'exit_code': None})
        poll = AsyncMock(return_value={'id': 'process-1', 'exit_code': 1, 'output': 'tests failed'})
        tools = {'run_command': {'callable': run}, 'get_process_status': {'callable': poll,
            'spec': {'parameters': {'properties': {'id': {}}}}}}
        result = await terminal_command(tools, '/workspace/owner', 'pytest', lambda value: value)
        self.assertEqual(result['exit_code'], 1)
        poll.assert_awaited_once_with(id='process-1', wait='10')


if __name__ == '__main__':
    unittest.main()
