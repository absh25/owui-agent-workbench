import asyncio
from contextlib import asynccontextmanager, ExitStack
import json
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch

from supervisor.coding_tool import Tools
from supervisor.verification import AcceptanceVerifier, run_verified_session


def inventory(value='original'):
    return {'files': {'calculator.py': value, 'test_calculator.py': 'fixed-tests'}, 'scripts': {}}


class Result:
    def __init__(self, success=True):
        self.result = json.dumps({'summary': '修复完成', 'changed_files': ['calculator.py'],
                                  'tests': ['python -m unittest -v'], 'open_issues': []})
        self.subtype = 'success' if success else 'error_max_turns'
        self.is_error = not success
        self.session_id = 'same-session'
        self.duration_ms = 12
        self.duration_api_ms = 8
        self.num_turns = 2


class Client:
    def __init__(self, results):
        self.results = iter(results)
        self.prompts = []

    async def query(self, prompt):
        self.prompts.append(prompt)
        await asyncio.sleep(0.002)

    async def receive_response(self):
        yield next(self.results)


class CodingTimingsTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_checks_and_final_snapshot_keep_phase_times(self):
        events = []

        async def take_snapshot(*args):
            await asyncio.sleep(0.002)
            return inventory('fixed')

        async def command(*args):
            await asyncio.sleep(0.002)
            return {'exit_code': 0, 'output': 'Ran 2 tests\nOK'}

        async def emit(description):
            events.append(description)

        verifier = AcceptanceVerifier({}, '/workspace/owner', lambda value: value, inventory(),
            ['python -m unittest -v'], '修复加法，不要修改测试。', '', emit=emit)
        with patch('supervisor.verification.snapshot', side_effect=take_snapshot) as snapshots, \
             patch('supervisor.verification.terminal_command', side_effect=command) as checks:
            report = await verifier.verify({})
        self.assertTrue(report['passed'])
        self.assertEqual(snapshots.await_count, 2)
        checks.assert_awaited_once()
        for field in ('snapshot_before_ms', 'tests_ms', 'snapshot_after_ms', 'total_ms'):
            self.assertGreater(report['timings'][field], 0)
        self.assertEqual(report['timings']['render_ms'], 0)
        self.assertEqual(report['timings']['visual_review_ms'], 0)
        self.assertGreater(report['checks'][0]['duration_ms'], 0)
        self.assertTrue(any('固定测试' in event for event in events))
        self.assertTrue(any('最终文件' in event for event in events))

    async def test_failed_phase_is_timed_without_claiming_later_checks(self):
        async def fail_snapshot(*args):
            await asyncio.sleep(0.002)
            raise RuntimeError('snapshot unavailable')

        verifier = AcceptanceVerifier({}, '/workspace/owner', lambda value: value, inventory(),
            ['python -m unittest -v'], '修复', '')
        with patch('supervisor.verification.snapshot', side_effect=fail_snapshot), \
             patch('supervisor.verification.terminal_command', new_callable=AsyncMock) as checks:
            report = await verifier.verify({})
        self.assertFalse(report['passed'])
        self.assertGreater(report['timings']['snapshot_before_ms'], 0)
        self.assertGreater(report['timings']['total_ms'], 0)
        self.assertEqual(report['timings']['tests_ms'], 0)
        checks.assert_not_awaited()

    async def test_browser_and_visual_review_remain_separate_required_phases(self):
        async def render(*args):
            await asyncio.sleep(0.002)
            return {'passed': True, 'issues': [], 'source_sha256': 'fixed',
                    'screenshots': [{'name': 'desktop.png', 'data': 'actual-image'}]}

        async def review(*args):
            await asyncio.sleep(0.002)
            return {'passed': False, 'issues': ['主体被裁切'], 'checked': ['主体构图']}

        verifier = AcceptanceVerifier({}, '/workspace/owner', lambda value: value,
            {'files': {}, 'scripts': {}}, [], '绘图', '', preview_target='cat.svg', visual_model='vision')
        with patch('supervisor.verification.snapshot', AsyncMock(return_value={
                'files': {'cat.svg': 'fixed'}, 'scripts': {}})) as snapshots, \
             patch('supervisor.verification.render_target', side_effect=render) as browser, \
             patch('supervisor.verification.review_screenshots', side_effect=review) as visual:
            report = await verifier.verify({})
        self.assertFalse(report['passed'])
        self.assertIn('主体被裁切', report['issues'])
        self.assertEqual(snapshots.await_count, 2)
        browser.assert_awaited_once()
        visual.assert_awaited_once()
        self.assertGreater(report['timings']['render_ms'], 0)
        self.assertGreater(report['timings']['visual_review_ms'], 0)

    async def test_progress_transport_failure_cannot_replace_acceptance(self):
        verifier = AcceptanceVerifier({}, '/workspace/owner', lambda value: value, inventory(),
            ['python -m unittest -v'], '修复', '',
            emit=AsyncMock(side_effect=RuntimeError('browser disconnected')))
        with patch('supervisor.verification.snapshot', AsyncMock(return_value=inventory('fixed'))) as snapshots, \
             patch('supervisor.verification.terminal_command', AsyncMock(return_value={
                 'exit_code': 0, 'output': 'Ran 2 tests\nOK'})) as checks:
            report = await verifier.verify({})
        self.assertTrue(report['passed'])
        self.assertEqual(snapshots.await_count, 2)
        checks.assert_awaited_once()

    async def test_repair_attempts_keep_separate_agent_and_verification_times(self):
        outcomes = iter([{'passed': False, 'issues': ['AssertionError'], 'checks': [{'exit_code': 1}]},
                         {'passed': True, 'issues': [], 'checks': [{'exit_code': 0}],
                          'timings': {'tests_ms': 1.5}}])

        async def verify(*args):
            await asyncio.sleep(0.002)
            return next(outcomes)

        verifier = NS(images=[], verify=verify)
        client = Client([Result(), Result()])
        _, report = await run_verified_session(client, 'task', verifier, Result, 2, AsyncMock())
        self.assertTrue(report['passed'])
        self.assertEqual(len(verifier.attempt_timings), 2)
        self.assertEqual([item['attempt'] for item in verifier.attempt_timings], [1, 2])
        self.assertIn('AssertionError', client.prompts[1])
        for attempt in verifier.attempt_timings:
            self.assertGreater(attempt['agent_ms'], 0)
            self.assertGreater(attempt['verification_ms'], 0)
            self.assertEqual(attempt['sdk_num_turns'], 2)
            self.assertEqual(attempt['sdk_reported_ms'], 12)
            self.assertEqual(attempt['sdk_api_reported_ms'], 8)
        self.assertEqual(report['history'][0]['timings']['attempt'], 1)
        self.assertEqual(report['history'][1]['timings']['attempt'], 2)
        self.assertEqual(report['history'][1]['timings']['verification_phases']['tests_ms'], 1.5)

    async def test_sdk_failure_keeps_agent_time_without_a_fake_verification(self):
        result = Result(success=False)
        result.duration_api_ms = float('nan')
        verifier = NS(images=[], verify=AsyncMock())
        _, report = await run_verified_session(Client([result]), 'task', verifier, Result, 2, AsyncMock())
        self.assertIsNone(report)
        verifier.verify.assert_not_awaited()
        self.assertEqual(len(verifier.attempt_timings), 1)
        self.assertGreater(verifier.attempt_timings[0]['agent_ms'], 0)
        self.assertIsNone(verifier.attempt_timings[0]['verification_ms'])
        self.assertNotIn('sdk_api_reported_ms', verifier.attempt_timings[0])

    async def test_invalid_arguments_report_only_preflight_without_starting_sdk(self):
        with patch('supervisor.coding_tool.ClaudeSDKClient') as sdk:
            report = await Tools().delegate_coding_task('修复', verification_commands=['echo passed'])
        self.assertEqual(report['status'], 'invalid_arguments')
        self.assertFalse(report['execution_started'])
        self.assertGreaterEqual(report['timings']['preflight_ms'], 0)
        self.assertGreaterEqual(report['timings']['total_ms'], report['timings']['preflight_ms'])
        self.assertEqual(report['timings']['attempts'], [])
        self.assertEqual(report['timings']['sdk_and_verification_ms'], 0)
        sdk.assert_not_called()

    async def delegate_fixture(self, *, startup_failure=False, broken_emitter=False):
        timeline, captured, adapted = [], {}, {}

        async def emit(event):
            timeline.append(('status', event['data']))
            if broken_emitter:
                raise RuntimeError('status disconnected')

        async def run_command(**params):
            await asyncio.sleep(0.002)
            return {'exit_code': 0, 'output': ''}

        async def write_file(**params):
            timeline.append(('tool', 'write_file'))
            await asyncio.sleep(0.002)
            return {'status': 'ok'}

        terminal = {'run_command': {'callable': run_command, 'spec': {
            'description': 'The current working directory is: /workspace/owner'}},
            'write_file': {'callable': write_file, 'spec': {}}}

        def build_tools(tools):
            adapted.update(tools)
            return None, ['mcp__openwebui__write_file'], {}

        @asynccontextmanager
        async def bridge(*args):
            await asyncio.sleep(0.002)
            yield

        class SDK:
            def __init__(self, *, options):
                captured['options'] = options

            async def __aenter__(self):
                await asyncio.sleep(0.002)
                if startup_failure:
                    raise RuntimeError('SDK startup unavailable')
                return self

            async def __aexit__(self, *args):
                pass

            async def query(self, prompt):
                captured['prompt'] = prompt
                await adapted['write_file']['callable'](path='calculator.py', content='PRIVATE_IMPLEMENTATION')

            async def receive_response(self):
                yield Result()

        async def deliver(*args):
            timeline.append(('delivery', None))
            await asyncio.sleep(0.002)
            return [], []

        lock = asyncio.Lock()
        await lock.acquire()

        async def release_lock():
            await asyncio.sleep(0.008)
            lock.release()

        release = asyncio.create_task(release_lock())
        with ExitStack() as stack:
            stack.enter_context(patch('open_webui.models.users.Users.get_user_by_id',
                                      AsyncMock(return_value=NS(id='owner'))))
            stack.enter_context(patch('open_webui.utils.tools.get_terminal_tools', AsyncMock(return_value=terminal)))
            stack.enter_context(patch('supervisor.coding_tool._workspace_locks', {'timing-demo': lock}))
            stack.enter_context(patch('supervisor.coding_tool.Path'))
            stack.enter_context(patch('supervisor.coding_tool._build_owui_mcp_server', side_effect=build_tools))
            stack.enter_context(patch('supervisor.coding_tool._sdk_tool_name_bridge', bridge))
            stack.enter_context(patch('supervisor.coding_tool.ClaudeSDKClient', SDK))
            stack.enter_context(patch('supervisor.coding_tool.ClaudeAgentOptions', lambda **options: options))
            stack.enter_context(patch('supervisor.coding_tool.ResultMessage', Result))
            stack.enter_context(patch('supervisor.coding_tool.snapshot', AsyncMock(return_value=inventory())))
            stack.enter_context(patch('supervisor.verification.snapshot', AsyncMock(return_value=inventory('fixed'))))
            stack.enter_context(patch('supervisor.verification.terminal_command', AsyncMock(return_value={
                'exit_code': 0, 'output': 'Ran 2 tests\nOK'})))
            stack.enter_context(patch('supervisor.coding_tool.deliver_html', side_effect=deliver))
            report = await Tools().delegate_coding_task('修复 calculator.py，不要修改测试。',
                verification_commands=['python -m unittest -v'], __request__=object(),
                __user__={'id': 'owner'}, __metadata__={'terminal_id': 'timing-demo'},
                __event_emitter__=emit)
        await release
        return report, timeline, captured

    async def test_full_delegate_reports_real_stages_and_done_after_delivery(self):
        report, timeline, captured = await self.delegate_fixture()
        self.assertEqual(report['status'], 'completed')
        self.assertTrue(report['verification']['passed'])
        timings = report['timings']
        self.assertGreater(timings['preflight_ms'], 0)
        self.assertGreater(timings['lock_wait_ms'], 0)
        self.assertGreater(timings['sdk_startup_ms'], 0)
        self.assertGreater(timings['delivery_ms'], 0)
        self.assertGreaterEqual(timings['sdk_and_verification_ms'], timings['sdk_startup_ms'])
        self.assertGreaterEqual(timings['total_ms'], timings['sdk_and_verification_ms'])
        self.assertEqual(timings['tools'][0]['tool'], 'write_file')
        self.assertGreater(timings['tools'][0]['duration_ms'], 0)
        self.assertEqual(timings['tool_total_ms'], timings['tools'][0]['duration_ms'])
        self.assertEqual(report['tool_evidence'][0]['duration_ms'], timings['tool_total_ms'])
        self.assertNotIn('PRIVATE_IMPLEMENTATION', json.dumps(timings))
        tool_index = next(index for index, item in enumerate(timeline) if item[0] == 'tool')
        self.assertIn('正在运行', timeline[tool_index - 1][1]['description'])
        delivery_index = next(index for index, item in enumerate(timeline) if item[0] == 'delivery')
        done_indices = [index for index, item in enumerate(timeline) if item[0] == 'status' and item[1]['done']]
        self.assertEqual(len(done_indices), 1)
        self.assertGreater(done_indices[0], delivery_index)
        self.assertIn('最小实现', captured['options']['system_prompt'])
        self.assertEqual(captured['options']['max_turns'], 20)

    async def test_sdk_startup_failure_keeps_timings_and_actual_failed_status(self):
        report, _, _ = await self.delegate_fixture(startup_failure=True)
        self.assertEqual(report['status'], 'failed')
        self.assertGreater(report['timings']['sdk_startup_ms'], 0)
        self.assertGreater(report['timings']['sdk_and_verification_ms'], 0)
        self.assertEqual(report['timings']['attempts'], [])
        self.assertEqual(report['timings']['delivery_ms'], 0)

    async def test_status_disconnect_does_not_skip_fixed_tests_or_delivery(self):
        report, timeline, _ = await self.delegate_fixture(broken_emitter=True)
        self.assertEqual(report['status'], 'completed')
        self.assertEqual(report['verification']['checks'][0]['exit_code'], 0)
        self.assertTrue(any(item[0] == 'delivery' for item in timeline))


if __name__ == '__main__':
    unittest.main()
