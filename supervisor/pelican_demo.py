"""Explicit prepared-scene delivery with real workspace and browser checks.

This path reuses a reviewed fixture. It does not invoke the SDK, generate new
code, or rerun a visual language model. Ordinary coding keeps its own verifier.
"""

import asyncio
import hashlib
from html.parser import HTMLParser
import json
import math
from pathlib import Path
import re
import shlex
import time

from supervisor.artifacts import MAX_HTML_BYTES, deliver_html, save_screenshot
from supervisor.verification import changed_paths, elapsed_ms, render_target, snapshot, timed_phase, terminal_command


SCENE = 'pelican_bike'
FILENAME = 'pelican-riding-bike.html'
ASSET_DIRECTORY = Path(__file__).with_name('demo_assets')


def validate_scene_request(scene, enabled, read_only, commands, preview_target, context, acceptance):
    if not isinstance(scene, str) or scene not in {'', SCENE}:
        raise ValueError("demo_scene 仅支持空字符串或 pelican_bike。")
    if not scene:
        return
    if not enabled:
        raise ValueError("预制鹈鹕演示未启用；请使用 demo_scene='' 走普通编码流程。")
    if read_only or commands or preview_target not in {'', FILENAME}:
        raise ValueError("预制演示只支持非只读任务、verification_commands=[]，preview_target 留空或为 pelican-riding-bike.html。")
    if (not isinstance(context, str) or not isinstance(acceptance, str)
            or context.strip() or acceptance.strip()):
        raise ValueError("预制场景不支持附加业务上下文或自定义验收要求；请使用 demo_scene='' 走普通编码流程。")


class _Identifiers(HTMLParser):
    def __init__(self):
        super().__init__()
        self.identifiers = []

    def handle_starttag(self, tag, attributes):
        self.identifiers.extend(value for name, value in attributes if name == 'id')


def load_prepared_scene(asset_directory=None):
    folder = Path(asset_directory) if asset_directory is not None else ASSET_DIRECTORY
    data = (folder / FILENAME).read_bytes()
    if not data or len(data) > MAX_HTML_BYTES:
        raise ValueError('预制文件为空或超过交付大小限制。')
    content = data.decode('utf-8')
    manifest = json.loads((folder / 'pelican-riding-bike.manifest.json').read_text(encoding='utf-8'))
    if not isinstance(manifest, dict) or manifest.get('scene') != SCENE:
        raise ValueError('预制场景清单无效。')
    digest = hashlib.sha256(data).hexdigest()
    if not re.fullmatch(r'[0-9a-f]{64}', str(manifest.get('source_sha256', ''))) or manifest['source_sha256'] != digest:
        raise ValueError('预制文件与录前复核的 SHA256 不一致，拒绝交付。')
    review = manifest.get('visual_review')
    if not isinstance(review, dict) or review.get('passed') is not True or not review.get('reviewed_at'):
        raise ValueError('预制文件缺少有效的录前视觉复核记录。')
    previous = manifest.get('previous_source_sha256', [])
    if (not isinstance(previous, list) or len(previous) > 5
            or any(not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{64}', value) for value in previous)):
        raise ValueError('预制场景历史版本哈希无效。')
    selectors = manifest.get('required_selectors')
    if (not isinstance(selectors, list) or not 1 <= len(selectors) <= 20
            or any(not isinstance(selector, str) or not re.fullmatch(r'#[A-Za-z][A-Za-z0-9_-]*', selector)
                   for selector in selectors) or len(selectors) != len(set(selectors))):
        raise ValueError('预制场景要求一到二十个唯一的 #id 源文件标记。')
    parser = _Identifiers()
    parser.feed(content)
    if any(parser.identifiers.count(selector[1:]) != 1 for selector in selectors):
        raise ValueError('预制文件缺少清单指定的唯一主体标记。')
    return content, manifest


async def wait_for_demo_window(started, return_seconds, emit, timings):
    waiting = time.monotonic()
    try:
        while True:
            remaining = started + return_seconds - time.monotonic()
            if remaining <= 0:
                return
            await emit(f'预制演示文件已完成检查，等待约定展示时刻；剩余约 {math.ceil(remaining)} 秒。')
            await asyncio.sleep(min(5, remaining))
    finally:
        timings['demo_wait_ms'] = elapsed_ms(waiting)


async def run_pelican_demo(*, task_id, directory, terminal_tools, coding_tools,
                           user_id, initial, unwrap, emit, timings, started, return_seconds):
    """Copy the exact reviewed scene, verify it, deliver it, then pace its display."""
    timings.update({'demo_verification_ms': 0, 'demo_wait_ms': 0})
    verification = {'passed': False, 'status': 'not_run', 'checks': [], 'browser': [],
                    'issues': [], 'changed_files': [], 'integrity_checks': [],
                    'timings': dict.fromkeys(('snapshot_before_ms', 'render_ms', 'snapshot_after_ms', 'total_ms'), 0)}
    report = {'status': 'failed', 'task_id': task_id, 'repo': directory,
              'execution_mode': 'demo_fixture', 'sdk_used': False, 'sdk_turns': 0,
              'prepared': True, 'scene': SCENE, 'execution_started': False,
              'summary': '', 'changed_files': [], 'tests': {'status': 'not_run',
                  'report': '预制场景不运行程序单测；运行时检查文件、浏览器与交付完整性。', 'command_results': []},
              'verification': verification, 'open_issues': [], 'artifacts': [], 'delivery_errors': []}
    images = []
    try:
        await emit('正在加载已完成录前复核的预制鹈鹕骑车场景；本模式不调用 Claude SDK。')
        content, manifest = load_prepared_scene()
        verification['prepared_visual_review'] = {
            **manifest['visual_review'], 'source': 'recorded_preflight', 'runtime_rerun': False}
        verification['integrity_checks'].append({'kind': 'prepared_source_sha256', 'passed': True,
            'source_sha256': manifest['source_sha256']})
        verification['integrity_checks'].append({'kind': 'prepared_source_markers', 'passed': True,
            'selectors': manifest['required_selectors'], 'scope': 'source_html_presence_only'})
        if not callable(coding_tools.get('write_file', {}).get('callable')) or not callable(coding_tools.get('read_file', {}).get('callable')):
            raise ValueError('当前授权终端缺少预制场景所需的文件读写工具。')
        # Snapshot deliberately excludes symlinks. Check the actual target as
        # well, and never replace an existing different user-authored file.
        check = ("import pathlib,hashlib; root=pathlib.Path(" + repr(directory) + ").resolve(strict=True); "
                 "p=root/" + repr(FILENAME) + "; assert not p.is_symlink(), 'demo target is a symlink'; "
                 "assert p.resolve().parent==root, 'demo target escapes project'; "
                 "assert not p.exists() or (p.is_file() and hashlib.sha256(p.read_bytes()).hexdigest()=="
                 + repr(manifest['source_sha256']) + " or p.is_file() and hashlib.sha256(p.read_bytes()).hexdigest() in "
                 + repr(manifest.get('previous_source_sha256', [])) + "), 'existing demo file has different content'")
        safe = await terminal_command(terminal_tools, directory, 'python -c ' + shlex.quote(check), unwrap)
        verification['integrity_checks'].append({'kind': 'target_not_symlink_or_foreign_file',
                                                 'passed': safe['exit_code'] == 0})
        if safe['exit_code'] != 0:
            raise ValueError('预制文件路径为符号链接或已有不同作品，拒绝覆盖；请使用空闲的项目目录。')
        await emit('正在将预制 HTML 写入你的授权工作区…')
        report['execution_started'] = True
        written = unwrap(await coding_tools['write_file']['callable'](path=FILENAME, content=content))
        if isinstance(written, dict) and (written.get('error') or written.get('isError') or written.get('is_error')):
            raise ValueError('终端未能写入预制文件。')
        phase_started = time.monotonic()
        try:
            await emit('预制场景运行时验收：正在核对实际文件与记录状态…')
            with timed_phase(verification['timings'], 'snapshot_before_ms'):
                current = await snapshot(terminal_tools, directory, unwrap)
            changes = changed_paths(initial, current)
            report['changed_files'] = verification['changed_files'] = changes
            if current['files'].get(FILENAME) != manifest['source_sha256']:
                raise ValueError('工作区文件与已复核预制文件的 SHA256 不一致。')
            payload = unwrap(await coding_tools['read_file']['callable'](path=str(Path(directory) / FILENAME)))
            if (not isinstance(payload, dict) or not isinstance(payload.get('content'), str)
                    or hashlib.sha256(payload['content'].encode('utf-8')).hexdigest() != manifest['source_sha256']):
                raise ValueError('实际预制文件读取不完整或内容不一致。')
            verification['integrity_checks'].append({'kind': 'runtime_file_sha256', 'passed': True,
                'source_sha256': manifest['source_sha256']})
            await emit('预制场景运行时验收：正在采集两张桌面帧与手机截图…')
            with timed_phase(verification['timings'], 'render_ms'):
                browser = await render_target(directory, FILENAME)
            images = browser.pop('screenshots')
            verification['browser'].append({'target': FILENAME, **browser})
            if browser.get('passed') is not True:
                verification['issues'].extend(str(issue) for issue in browser.get('issues') or ['浏览器检查未通过。'])
            if browser.get('source_sha256') != manifest['source_sha256']:
                verification['issues'].append('运行时截图对应源文件与录前复核的 SHA256 不一致。')
            await emit('预制场景运行时验收：正在确认最终文件一致性…')
            with timed_phase(verification['timings'], 'snapshot_after_ms'):
                final = await snapshot(terminal_tools, directory, unwrap)
            if current['files'] != final['files']:
                verification['issues'].append('验收期间项目文件发生变化，当前结果不能交付。')
            verification['integrity_checks'].append({'kind': 'final_snapshot_unchanged',
                'passed': current['files'] == final['files']})
            verification['passed'] = not verification['issues']
            verification['status'] = 'passed' if verification['passed'] else 'failed'
        finally:
            timings['demo_verification_ms'] = elapsed_ms(phase_started)
            verification['timings']['total_ms'] = timings['demo_verification_ms']
        if not verification['passed']:
            report.update(status='verification_failed', stage='verification',
                          summary='预制场景的真实运行时验收未通过，未完成交付。')
            report['open_issues'] = list(verification['issues'])
            await emit(report['summary'], True)
            return report
        report.update(status='completed', summary='已交付预制鹈鹕骑车演示；文件、桌面与手机截图及最终一致性已实际检查。')
        await emit('预制场景运行时验收通过，正在保存三张真实截图和 HTML 附件…')
        with timed_phase(timings, 'delivery_ms'):
            verification['screenshots'] = []
            for image in images:
                try:
                    verification['screenshots'].append(await save_screenshot(user_id, image))
                except Exception as exc:
                    report['delivery_errors'].append({'path': image.get('name', ''), 'error': str(exc)})
            report['artifacts'], errors = await deliver_html(report, terminal_tools, user_id, directory, unwrap)
            report['delivery_errors'].extend(errors)
        if report['delivery_errors'] or not report['artifacts'] or len(verification['screenshots']) != 3:
            report.update(status='failed', stage='delivery', summary='预制文件验收通过，但截图或 HTML 附件交付尚未完成。')
            await emit(report['summary'], True)
            return report
        await wait_for_demo_window(started, return_seconds, emit, timings)
        await emit('预制鹈鹕骑车演示已检查并交付；本次未调用 Claude SDK。', True)
        return report
    except Exception as exc:
        verification['passed'] = False
        verification['issues'].append(str(exc))
        verification['status'] = 'failed'
        report.update(status='failed', summary='预制演示未完成：' + str(exc))
        report['open_issues'] = list(verification['issues'])
        await emit(report['summary'], True)
        return report
