"""Exercise standalone SVG and the real chat -> coding -> verified artifact path."""

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import shlex
import uuid
from xml.etree import ElementTree

import httpx
from starlette.requests import Request
from open_webui.main import app
from open_webui.models.users import Users
from open_webui.utils.auth import create_token
from open_webui.utils.tools import get_terminal_tools

from claude_agent_pipe import _unwrap_owui_tool_result
from supervisor.artifacts import previews
from supervisor.smoke_agents import saved_chat, tool_results
from supervisor.verification import render_target, terminal_command


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--live', action='store_true')
    args = parser.parse_args()
    owner = os.environ.get('OPEN_TERMINAL_OWNER_NAME', 'wenguang.wu')
    users = await Users.get_users(limit=1000)
    user = next(candidate for candidate in users['users'] if candidate.name == owner)
    request = Request({'type': 'http', 'app': app, 'method': 'GET', 'path': '/', 'headers': [],
        'query_string': b'', 'server': ('test', 80), 'client': ('test', 12345), 'scheme': 'http'})
    value = await get_terminal_tools(request, 'local-open-terminal', user, {'__user__': user.model_dump()})
    terminal = value[0] if isinstance(value, tuple) else value
    workspace = '/workspace/wenguang.wu'
    directory = workspace + '/cat-delivery-smoke-' + uuid.uuid4().hex[:12]
    svg = '<svg xmlns="http://www.w3.org/2000/svg" width="400" height="400" viewBox="0 0 400 400"><script>throw new Error("SVG script must not execute")</script><circle cx="200" cy="200" r="70" fill="none" stroke="black" stroke-width="4"/></svg>'
    source = 'import pathlib; p=pathlib.Path(' + repr(directory) + '); p.mkdir(exist_ok=False); (p/"probe.svg").write_text(' + repr(svg) + ',encoding="utf-8")'
    result = await terminal_command(terminal, workspace, 'python -c ' + shlex.quote(source), _unwrap_owui_tool_result)
    assert result['exit_code'] == 0
    probe = await render_target(directory, 'probe.svg')
    assert probe['passed'], probe['issues']
    assert probe['source_sha256'] == hashlib.sha256(svg.encode()).hexdigest()
    assert len(probe['screenshots']) == 3
    assert all(item['scrollWidth'] <= item['width'] + 3 for item in probe['measurements'])
    print('SVG_BROWSER_VERIFIED', json.dumps({key: item for key, item in probe.items() if key != 'screenshots'}), flush=True)
    if not args.live:
        return
    prompt = '帮我画一幅简笔画吧，就是猫咪的。请在 ' + directory + ' 项目中保存为 cat_doodle.svg，并提供我能看到的预览和可下载的原文件。'
    message = await saved_chat(user, prompt)
    calls = [item for item in message.get('output', []) if item.get('type') == 'function_call']
    assert calls and all(call['name'].endswith('delegate_coding_task') for call in calls), 'Unexpected fallback or business tool'
    results = tool_results(message)
    report = next(result for result in results.values() if isinstance(result, dict) and result.get('status') == 'completed')
    assert report['verification']['passed'] is True and report['artifacts']
    assert not report['delivery_errors'], report['delivery_errors']
    artifact = next(item for item in report['artifacts'] if item['name'].endswith('.svg'))
    async with httpx.AsyncClient(base_url='http://127.0.0.1:8080', timeout=20,
        headers={'Authorization': 'Bearer ' + create_token({'id': user.id})}) as client:
        response = await client.get(artifact['url'])
        response.raise_for_status()
        data = response.content
        assert ElementTree.fromstring(data).tag.endswith('svg')
        accepted = next(item for item in report['verification']['browser'] if item['target'].endswith('.svg'))
        assert hashlib.sha256(data).hexdigest() == accepted['source_sha256']
        screenshot = report['verification']['screenshots'][0]
        rendered = await client.get(screenshot['url'])
        rendered.raise_for_status()
        assert rendered.content.startswith(b'\x89PNG')
    messages = [{'role': 'user', 'content': prompt}, {'role': 'assistant', 'tool_calls': [
        {'id': 'coding', 'function': {'name': 'delegate_coding_task'}}]},
        {'role': 'tool', 'tool_call_id': 'coding', 'content': json.dumps(report)}]
    markup = await previews(messages, user.id)
    assert 'data:image/svg+xml;base64,' in markup and artifact['url'] in markup
    assert 'data:image/svg+xml;base64,' in message.get('content', ''), 'The actual chat omitted SVG preview'
    export = Path('/app/backend/data/cat-delivery-exports') / Path(directory).name
    export.mkdir(parents=True, exist_ok=False)
    (export / 'cat_doodle.svg').write_bytes(data)
    (export / 'cat-preview.png').write_bytes(rendered.content)
    print('CAT_DELIVERY_VERIFIED', json.dumps({'status': report['status'], 'fixture': directory,
        'main_tool_calls': [call['name'] for call in calls], 'verification_attempts': report['verification']['attempts'],
        'visual_review': report['verification'].get('visual_review'), 'artifact': artifact,
        'screenshot': screenshot, 'export': str(export), 'preview_verified': True}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    asyncio.run(main())
