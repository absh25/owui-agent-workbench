import asyncio
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import AsyncMock, patch

from supervisor.coding_tool import Tools
from supervisor.pelican_demo import load_prepared_scene, run_pelican_demo, validate_scene_request, wait_for_demo_window


class PelicanDemoTests(unittest.IsolatedAsyncioTestCase):
    def test_prepared_source_matches_reviewed_manifest(self):
        content, manifest = load_prepared_scene()
        self.assertEqual(hashlib.sha256(content.encode()).hexdigest(), manifest['source_sha256'])
        self.assertTrue(manifest['visual_review']['passed'])

    def test_changed_fixture_or_missing_source_markers_cannot_claim_preapproval(self):
        content, manifest = load_prepared_scene()
        with TemporaryDirectory() as temporary:
            folder = Path(temporary)
            file = folder / 'pelican-riding-bike.html'
            file.write_bytes(content.encode() + b'changed')
            (folder / 'pelican-riding-bike.manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'SHA256'):
                load_prepared_scene(folder)
            file.write_bytes(content.encode())
            manifest['required_selectors'] = ['#missing']
            (folder / 'pelican-riding-bike.manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, '主体标记'):
                load_prepared_scene(folder)

    def test_demo_cannot_replace_standard_task_or_custom_acceptance(self):
        base = ['pelican_bike', True, False, [], 'pelican-riding-bike.html', '', '']
        validate_scene_request(*base)
        for index, value in ((0, 'cat'), (1, False), (2, True), (3, ['python -m unittest']),
                             (4, 'other.html'), (5, 'new requirement'), (6, 'new acceptance')):
            arguments = base.copy()
            arguments[index] = value
            with self.assertRaises(ValueError):
                validate_scene_request(*arguments)
        validate_scene_request('', False, False, ['python -m unittest'], '', '', '')

    async def test_disabled_demo_does_not_start_sdk(self):
        with patch('supervisor.coding_tool.ClaudeSDKClient') as sdk:
            result = await Tools().delegate_coding_task('鹈鹕骑车', demo_scene='pelican_bike')
        self.assertEqual(result['status'], 'invalid_arguments')
        self.assertFalse(result['execution_started'])
        sdk.assert_not_called()

    async def invoke(self, *, browser_passed=True, changed_during=False, wrong_sha=False,
                     target_safe=True, attachment_error=False, reused=False):
        content, manifest = load_prepared_scene()
        digest = manifest['source_sha256']
        current = {'files': {'pelican-riding-bike.html': digest}, 'scripts': {}}
        initial = current if reused else {'files': {}, 'scripts': {}}
        final = {'files': {**current['files'], 'foreign.py': 'changed'}, 'scripts': {}} if changed_during else current
        browser = {'passed': browser_passed, 'issues': [] if browser_passed else ['页面空白'],
                   'source_sha256': 'bad' if wrong_sha else digest,
                   'screenshots': [{'name': str(i)} for i in range(3)]}
        tools = {'write_file': {'callable': AsyncMock(return_value={'status': 'ok'})},
                 'read_file': {'callable': AsyncMock(return_value={'content': content})}}
        emitter = AsyncMock()
        with patch('supervisor.pelican_demo.snapshot', AsyncMock(side_effect=[current, final])), \
                patch('supervisor.pelican_demo.render_target', AsyncMock(return_value=browser)), \
                patch('supervisor.pelican_demo.terminal_command', AsyncMock(return_value={'exit_code': 0 if target_safe else 1})), \
                patch('supervisor.pelican_demo.save_screenshot', AsyncMock(return_value={'file_id': 'shot'})), \
                patch('supervisor.pelican_demo.deliver_html', AsyncMock(return_value=(
                    [] if attachment_error else [{'file_id': 'html'}], [{'error': 'save failed'}] if attachment_error else []))), \
                patch('supervisor.pelican_demo.wait_for_demo_window', AsyncMock()) as waiting:
            report = await run_pelican_demo(task_id='task', directory='/workspace/owner', terminal_tools={},
                coding_tools=tools, user_id='owner', initial=initial, unwrap=lambda value: value,
                emit=emitter, timings={}, started=time.monotonic(), return_seconds=30)
        return report, tools, waiting

    async def test_real_checks_and_attachments_are_required_without_sdk(self):
        report, tools, waiting = await self.invoke()
        self.assertEqual(report['status'], 'completed')
        self.assertTrue(report['verification']['passed'])
        self.assertFalse(report['sdk_used'])
        self.assertEqual(report['execution_mode'], 'demo_fixture')
        self.assertEqual(len(report['verification']['screenshots']), 3)
        tools['write_file']['callable'].assert_awaited_once()
        waiting.assert_awaited_once()

    async def test_browser_failure_source_drift_or_project_mutation_cannot_deliver_success(self):
        for kwargs in ({'browser_passed': False}, {'wrong_sha': True}, {'changed_during': True}):
            with self.subTest(**kwargs):
                report, _, waiting = await self.invoke(**kwargs)
                self.assertEqual(report['status'], 'verification_failed')
                self.assertFalse(report['verification']['passed'])
                self.assertEqual(report['artifacts'], [])
                waiting.assert_not_awaited()

    async def test_symlink_or_foreign_existing_file_is_rejected_before_write(self):
        report, tools, waiting = await self.invoke(target_safe=False)
        self.assertEqual(report['status'], 'failed')
        self.assertFalse(report['verification']['passed'])
        tools['write_file']['callable'].assert_not_awaited()
        waiting.assert_not_awaited()

    async def test_delivery_failure_returns_failure_without_pacing(self):
        report, _, waiting = await self.invoke(attachment_error=True)
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(report['stage'], 'delivery')
        waiting.assert_not_awaited()

    async def test_repeated_scene_still_verifies_and_delivers_unchanged_file(self):
        report, _, waiting = await self.invoke(reused=True)
        self.assertEqual(report['changed_files'], [])
        self.assertEqual(report['status'], 'completed')
        self.assertTrue(report['artifacts'])
        waiting.assert_awaited_once()

    async def test_wait_uses_remaining_window_and_does_not_restart_after_slow_checks(self):
        emitter, timings = AsyncMock(), {}
        await wait_for_demo_window(time.monotonic() - 60, 30, emitter, timings)
        emitter.assert_not_awaited()
        self.assertIn('demo_wait_ms', timings)
        await wait_for_demo_window(time.monotonic(), .02, emitter, timings)
        self.assertIn('预制演示文件已完成检查', emitter.call_args.args[0])
        self.assertGreaterEqual(timings['demo_wait_ms'], 15)

    async def test_wait_cancellation_propagates(self):
        timings = {}
        task = asyncio.create_task(wait_for_demo_window(time.monotonic(), 30, AsyncMock(), timings))
        await asyncio.sleep(.002)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertIn('demo_wait_ms', timings)


if __name__ == '__main__':
    unittest.main()
