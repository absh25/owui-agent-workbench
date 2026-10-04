"""Verify deployed registry, browser boundary, attachments, and real UI handoff."""

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path

import httpx
from open_webui.models.functions import Functions
from open_webui.models.tools import Tools
from open_webui.models.users import Users
from open_webui.utils.auth import create_token

from supervisor.artifacts import previews
from supervisor.smoke_agents import saved_chat, tool_results


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--handoff', action='store_true')
    args = parser.parse_args()
    root = Path(__file__).parent
    tool = await Tools.get_tool_by_id('coding_agent')
    main = await Functions.get_function_by_id('owui-main')
    assert tool.content == (root / 'coding_tool.py').read_text(encoding='utf-8')
    assert main.content == (root / 'native_pipe.py').read_text(encoding='utf-8')
    fields = tool.specs[0]['parameters']['properties']
    assert {'verification_commands', 'preview_target', 'read_only'} <= fields.keys()
    users = await Users.get_users(limit=1000)
    owner = os.environ.get('OPEN_TERMINAL_OWNER_NAME', 'wenguang.wu')
    user = next(candidate for candidate in users['users'] if candidate.name == owner)
    screenshots = [
        ('953ba45a-4012-4821-8378-561e8ce7f4a6', '071befbabceda1c73c0faaa1988f3eac743a664aecaa43e8045d7d11beffb552'),
        ('8b726ce5-ac99-4b57-87a0-8e448f6bb677', 'cb1ddd8510dcc80e6fb6f64554bdcd876c734f6b9e6c0d1d393b8306a7b6cfa5')]
    async with httpx.AsyncClient(base_url='http://127.0.0.1:8080', timeout=20,
        headers={'Authorization': 'Bearer ' + create_token({'id': user.id})}) as client:
        for file_id, expected in screenshots:
            response = await client.get('/api/v1/files/' + file_id + '/content')
            response.raise_for_status()
            assert hashlib.sha256(response.content).hexdigest() == expected
    messages = [{'role': 'user', 'content': '网页任务'}, {'role': 'assistant', 'tool_calls': [
        {'id': 'coding', 'function': {'name': 'delegate_coding_task'}}]},
        {'role': 'tool', 'tool_call_id': 'coding', 'content': json.dumps({'status': 'completed',
            'verification': {'passed': True, 'screenshots': [{'file_id': item[0]} for item in screenshots]}})}]
    markdown = await previews(messages, user.id)
    assert all(file_id in markdown for file_id, _ in screenshots)
    async with httpx.AsyncClient(base_url='http://verification-browser:8088', timeout=10) as browser:
        assert (await browser.get('/health')).status_code == 200
        denied = await browser.post('/render', json={'directory': '/workspace/wenguang.wu', 'target': 'pelican_riding_bike.html'})
        assert denied.status_code == 401
        headers = {'Authorization': 'Bearer ' + os.environ['OPEN_TERMINAL_API_KEY']}
        denied = await browser.post('/render', headers=headers, json={'directory': '/etc', 'target': 'passwd'})
        assert denied.status_code == 400
        denied = await browser.post('/render', headers=headers,
            json={'directory': '/workspace/wenguang.wu', 'target': 'http://example.com/'})
        assert denied.status_code == 400
    print('RUNTIME_VERIFIED', json.dumps({'registered_parameters': list(fields), 'screenshots_downloaded': len(screenshots),
        'browser_boundaries_verified': True, 'main_model': (await Functions.get_function_valves_by_id('owui-main'))['MODEL'],
        'coding_model': (await Tools.get_tool_valves_by_id('coding_agent'))['MODEL']}), flush=True)
    if args.handoff:
        message = await saved_chat(user, '请只委派一次编码任务，核对 /workspace/wenguang.wu/verification-smoke-code-b20e08b92d55 中 calculator.py 的加法实现，并独立运行 python -m unittest -v。不要修改文件，read_only=true，verification_commands=["python -m unittest -v"]。不操作其他业务工具。根据真实验收结果回答。')
        results = tool_results(message)
        reports = [result for result in results.values() if isinstance(result, dict) and 'verification' in result]
        assert len(reports) == 1, 'Expected one coding verification result'
        result = reports[0]
        assert result['status'] == 'completed' and result['verification']['passed'] is True
        assert result['verification']['checks'][0]['exit_code'] == 0
        print('UI_HANDOFF_VERIFIED', json.dumps({'status': result['status'],
            'acceptance_checks': result['verification']['checks'], 'changed_files': result['changed_files']}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    asyncio.run(main())
