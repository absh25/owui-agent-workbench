import asyncio
import json
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch

from supervisor.coding_tool import Tools, workspace_lease
from supervisor.verification import AcceptanceVerifier, review_screenshots


class CodingQueueAndVisualTests(unittest.IsolatedAsyncioTestCase):
    async def test_queue_timeout_does_not_cancel_or_release_existing_holder(self):
        lock, held, release = asyncio.Lock(), asyncio.Event(), asyncio.Event()

        async def holder():
            async with lock:
                held.set()
                await release.wait()

        existing = asyncio.create_task(holder())
        await held.wait()
        timings = {}
        async with workspace_lease(lock, 0.02, timings) as acquired:
            self.assertFalse(acquired)
            self.assertTrue(lock.locked())
            self.assertFalse(existing.done())
        self.assertTrue(lock.locked())
        self.assertGreaterEqual(timings['lock_wait_ms'], 15)
        release.set()
        await existing
        self.assertFalse(lock.locked())

    async def test_lease_releases_on_success_and_execution_failure(self):
        for failure in (False, True):
            lock = asyncio.Lock()
            try:
                async with workspace_lease(lock, 1, {}) as acquired:
                    self.assertTrue(acquired)
                    self.assertTrue(lock.locked())
                    if failure:
                        raise RuntimeError('execution failed')
            except RuntimeError:
                self.assertTrue(failure)
            self.assertFalse(lock.locked())

    async def test_cancellation_releases_only_the_lease_it_acquired(self):
        lock, acquired = asyncio.Lock(), asyncio.Event()

        async def owner():
            async with workspace_lease(lock, 1, {}) as accepted:
                self.assertTrue(accepted)
                acquired.set()
                await asyncio.Event().wait()

        worker = asyncio.create_task(owner())
        await acquired.wait()
        worker.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await worker
        self.assertFalse(lock.locked())
        await lock.acquire()

        async def waiting():
            async with workspace_lease(lock, 1, {}):
                self.fail('Cancelled waiter must not enter the workspace')

        follower = asyncio.create_task(waiting())
        await asyncio.sleep(0.002)
        follower.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await follower
        self.assertTrue(lock.locked())
        lock.release()

    async def test_busy_report_is_not_sdk_failure_or_an_acceptance_attempt(self):
        lock = asyncio.Lock()
        await lock.acquire()
        terminal = {'run_command': {'callable': AsyncMock(), 'spec': {
            'description': 'The current working directory is: /workspace/owner'}}}
        events = []

        async def emit(event):
            events.append(event['data'])

        tool = Tools()
        # A short test-only wait exercises the real acquire timeout without
        # spending the production thirty-second queue budget in a unit test.
        tool.valves.WORKSPACE_WAIT_SECONDS = 0.02
        with patch('open_webui.models.users.Users.get_user_by_id', AsyncMock(return_value=NS(id='owner'))), \
             patch('open_webui.utils.tools.get_terminal_tools', AsyncMock(return_value=terminal)), \
             patch('supervisor.coding_tool._workspace_locks', {'busy-terminal': lock}), \
             patch('supervisor.coding_tool.snapshot', new_callable=AsyncMock) as snapshots, \
             patch('supervisor.coding_tool.ClaudeSDKClient') as sdk:
            report = await tool.delegate_coding_task('修复', verification_commands=['python -m unittest -v'],
                __request__=object(), __user__={'id': 'owner'},
                __metadata__={'terminal_id': 'busy-terminal'}, __event_emitter__=emit)
        self.assertEqual(report['status'], 'workspace_busy')
        self.assertFalse(report['execution_started'])
        self.assertFalse(report['verification']['passed'])
        self.assertEqual(report['verification']['status'], 'not_run')
        self.assertEqual(report['verification']['checks'], [])
        self.assertGreaterEqual(report['timings']['lock_wait_ms'], 15)
        self.assertEqual(report['timings']['sdk_and_verification_ms'], 0)
        self.assertEqual(report['timings']['attempts'], [])
        self.assertIn('尚未开始编码', report['summary'])
        self.assertTrue(any('另一项编码任务' in event['description'] for event in events))
        self.assertTrue(events[-1]['done'])
        self.assertTrue(lock.locked())
        snapshots.assert_not_awaited()
        sdk.assert_not_called()
        lock.release()

    async def model_review(self, report):
        class Model:
            def __init__(self):
                self.messages = NS(create=AsyncMock(return_value=NS(content=[
                    NS(text=json.dumps(report, ensure_ascii=False))])))

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

        with patch('supervisor.verification.AsyncAnthropic', return_value=Model()):
            return await review_screenshots('vision', '简单猫咪简笔画', '', ['cat.svg'], [])

    async def test_cosmetic_suggestions_do_not_block_valid_visual_result(self):
        report = await self.model_review({'passed': True, 'issues': [],
            'checked': ['猫咪主体可辨识、手机完整可见'], 'suggestions': ['可以选更柔和的配色']})
        self.assertTrue(report['passed'])
        self.assertEqual(report['suggestions'], ['可以选更柔和的配色'])

    async def test_real_required_defect_blocks_even_with_suggestions(self):
        report = await self.model_review({'passed': True, 'issues': ['用户要求的猫咪主体缺失'],
            'checked': ['主体核对'], 'suggestions': ['可以增加装饰']})
        self.assertFalse(report['passed'])
        self.assertTrue(report['issues'])

    async def test_optional_suggestions_are_backward_compatible_but_typed(self):
        report = await self.model_review({'passed': True, 'issues': [], 'checked': ['主体完整']})
        self.assertEqual(report['suggestions'], [])
        with self.assertRaises(ValueError):
            await self.model_review({'passed': True, 'issues': [], 'checked': ['主体完整'],
                                     'suggestions': 'invalid format'})

    async def test_verifier_keeps_advice_out_of_blocking_issues(self):
        verifier = AcceptanceVerifier({}, '/workspace/owner', lambda value: value,
            {'files': {}, 'scripts': {}}, [], '简单猫咪', '', preview_target='cat.svg')
        browser = {'passed': True, 'issues': [], 'source_sha256': 'accepted',
                   'screenshots': [{'name': 'desktop.png', 'data': 'image'}]}
        with patch('supervisor.verification.snapshot', AsyncMock(return_value={
                'files': {'cat.svg': 'accepted'}, 'scripts': {}})), \
             patch('supervisor.verification.render_target', AsyncMock(return_value=browser)), \
             patch('supervisor.verification.review_screenshots', AsyncMock(return_value={
                 'passed': True, 'issues': [], 'checked': ['主体完整'], 'suggestions': ['配色可优化']})):
            report = await verifier.verify({})
        self.assertTrue(report['passed'])
        self.assertEqual(report['issues'], [])
        self.assertEqual(report['suggestions'], ['配色可优化'])

    async def test_natural_test_preservation_requests_still_block_weakened_tests(self):
        original = {'files': {'calculator.py': 'old', 'test_calculator.py': 'original'}, 'scripts': {}}
        weakened = {'files': {'calculator.py': 'fixed', 'test_calculator.py': 'weakened'}, 'scripts': {}}
        for wording in ('测试文件不得修改', '测试保持不变', '不得修改原有测试', '测试文件不允许修改'):
            verifier = AcceptanceVerifier({}, '/workspace/owner', lambda value: value, original,
                ['python -m unittest -v'], '修复加法。' + wording, '')
            with patch('supervisor.verification.snapshot', AsyncMock(return_value=weakened)), \
                 patch('supervisor.verification.terminal_command', AsyncMock(return_value={
                     'exit_code': 0, 'output': 'Ran 2 tests\nOK'})):
                report = await verifier.verify({})
            self.assertFalse(report['passed'], wording)
            self.assertTrue(any('原有测试' in issue for issue in report['issues']))


if __name__ == '__main__':
    unittest.main()
