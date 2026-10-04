"""Real gate checks, including failure -> same SDK session repair -> acceptance."""

import argparse
import asyncio
import json
import os
from pathlib import PurePosixPath
import shlex
import uuid

from starlette.requests import Request
from open_webui.main import app
from open_webui.models.tools import Tools as Registry
from open_webui.models.users import Users
from open_webui.utils.tools import get_terminal_tools

from claude_agent_pipe import _unwrap_owui_tool_result
from supervisor.coding_tool import Tools
from supervisor.verification import render_target, review_screenshots, terminal_command


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--live', action='store_true')
    args = parser.parse_args()
    owner = os.environ.get('OPEN_TERMINAL_OWNER_NAME', 'wenguang.wu')
    users = await Users.get_users(limit=1000)
    user = next(candidate for candidate in users['users'] if candidate.name == owner)
    request = Request({'type': 'http', 'app': app, 'method': 'GET', 'path': '/', 'headers': [],
        'query_string': b'', 'server': ('test', 80), 'client': ('test', 12345), 'scheme': 'http'})
    metadata = {'chat_id': 'verification-smoke-' + uuid.uuid4().hex, 'message_id': uuid.uuid4().hex}
    value = await get_terminal_tools(request, 'local-open-terminal', user,
                                    {'__user__': user.model_dump(), '__metadata__': metadata})
    terminal = value[0] if isinstance(value, tuple) else value
    directory = '/workspace/wenguang.wu'
    saved = await Registry.get_tool_valves_by_id('coding_agent') or {}
    model = saved.get('VISUAL_REVIEW_MODEL') or saved['MODEL']
    browser = await render_target(directory, 'pelican_riding_bike.html')
    print('BROWSER_PROBE', json.dumps({key: value for key, value in browser.items() if key != 'screenshots'}, ensure_ascii=False), flush=True)
    review = await review_screenshots(model, '精美的鹈鹕骑车动态图',
        '角色应坐在座椅上，脚踩踏板，翅膀接触车把，动画各部分连接协调。',
        ['pelican_riding_bike.html'], browser['screenshots'])
    print('EXISTING_ANIMATION_REVIEW', json.dumps(review, ensure_ascii=False), flush=True)
    assert review['passed'] is False, 'The known detached rider must fail visual acceptance'
    if not args.live:
        return
    tool = Tools()
    tool.valves = tool.Valves(**saved)
    results = []
    for kind in ('code', 'web'):
        fixture = directory + '/verification-smoke-' + kind + '-' + uuid.uuid4().hex[:12]
        tests = 'import unittest\nfrom calculator import add\nclass Tests(unittest.TestCase):\n def test_add(self): self.assertEqual(add(2,3),5)\n def test_zero(self): self.assertEqual(add(-1,1),0)\n'
        contents = {'calculator.py': 'def add(a,b):\n return a-b\n', 'test_calculator.py': tests} if kind == 'code' else {
            'scene.html': '<!doctype html><html lang="zh"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><body style="margin:0;background:#fafafa;font-family:sans-serif"><main style="margin:40px;padding:30px;background:#dc2626;color:white"><h1>尚未通过</h1></main></body></html>'}
        source = 'import pathlib,json; p=pathlib.Path(' + repr(fixture) + '); p.mkdir(exist_ok=False); files=json.loads(' + repr(json.dumps(contents)) + '); [(p/name).write_text(text,encoding="utf-8") for name,text in files.items()]'
        prepared = await terminal_command(terminal, directory, 'python -c ' + shlex.quote(source), _unwrap_owui_tool_result)
        assert prepared['exit_code'] == 0
        changes = ('首次执行只读取 calculator.py，先不修改、不运行测试，立即返回 JSON。收到独立验收失败反馈后，'
                   '修复 add(a,b)，使其返回两数之和；不要修改 test_calculator.py。') if kind == 'code' else (
                   '首次执行只读取 scene.html，先不修改，立即返回 JSON。收到独立验收失败反馈和截图后，'
                   '将网页修成醒目的蓝色卡片，卡片内大标题必须为“验收通过”。手机也应完整显示。')
        task = '这是自动验收机制的受控自测，要求模拟首次未完成、收到反馈后再修复的流程。' + changes
        acceptance = 'add(2,3)=5，add(-1,1)=0；保留原有两项测试不变。' if kind == 'code' else '蓝色卡片内大标题为“验收通过”；桌面和手机均完整可见。'
        async def emit(event):
            if event.get('type') == 'status':
                print('LIVE_STATUS', kind, event['data']['description'], flush=True)
        result = await tool.delegate_coding_task(task=task, repo=fixture, acceptance=acceptance,
            verification_commands=['python -m unittest -v'] if kind == 'code' else [],
            preview_target='scene.html' if kind == 'web' else '', __request__=request,
            __user__=user.model_dump(), __metadata__=metadata, __event_emitter__=emit)
        verification = result.get('verification') or {}
        print('LIVE_RESULT', json.dumps({'kind': kind, 'status': result['status'], 'fixture': fixture,
            'attempts': verification.get('attempts'), 'issues': verification.get('issues'),
            'history': verification.get('history'), 'screenshots': verification.get('screenshots', []),
            'changed_files': result.get('changed_files')}, ensure_ascii=False), flush=True)
        assert result['status'] == 'completed', 'Live gate did not reach accepted completion'
        assert verification['attempts'] >= 2 and verification['history'][0]['passed'] is False
        assert verification['passed'] is True
        if kind == 'code':
            check = 'import pathlib; assert pathlib.Path("test_calculator.py").read_text()==' + repr(tests)
            checked = await terminal_command(terminal, fixture, 'python -c ' + shlex.quote(check), _unwrap_owui_tool_result)
            assert checked['exit_code'] == 0, 'Original tests changed'
        else:
            assert len(verification['screenshots']) == 3 and result['artifacts']
        results.append({'kind': kind, 'status': result['status'], 'attempts': verification['attempts']})
    print('LIVE_VERIFICATION_OK', json.dumps(results), flush=True)


if __name__ == '__main__':
    asyncio.run(main())
