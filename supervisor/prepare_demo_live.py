"""Prepare a marked demo fixture and index its explicitly fictional KB document.

Run inside the OWUI container with PYTHONPATH=/lab:/app/backend. No model query
is made. Indexing may call the configured embedding service. Reset only
overwrites this helper's two fixed files in an authenticated user's marked
demo-live directory; it never recursively deletes or edits other projects.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import uuid


DIRECTORY = "demo-live"
MARKER = ".owui-demo-live.json"
FIXTURE_ID = "owui-demo-window-v1"
IMPLEMENTATION = '"""Intentionally broken implementation for a real repair demonstration."""\n\ndef validate_window_size(value):\n    return value\n'
TESTS = '''import unittest
from demo_window_validator import validate_window_size


class WindowTests(unittest.TestCase):
    def test_legal_values(self):
        for value in (64, 256, 4096):
            with self.subTest(value=value):
                self.assertEqual(validate_window_size(value), value)

    def test_invalid_types(self):
        for value in (True, False, 256.0, "256", None):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    validate_window_size(value)

    def test_invalid_values(self):
        for value in (0, 63, 100, 4097, 8192):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    validate_window_size(value)


if __name__ == '__main__':
    unittest.main()
'''
DOCUMENT = '''# DEMO-PARAM-001：现场演示参数规范

本文是为 OWUI Agent Workbench 现场演示人工编写的虚构资料，仅用于演示知识检索、证据交接和程序验证。它不是行业标准，没有真实传感器实验数据，不保证真实性能。

## window_size 窗口参数

window_size 必须为 Python 整数，且明确排除 bool；合法范围为 64 到 4096，包含两端；必须是 2 的幂。默认值为 256。

validate_window_size(value) 对合法值返回原整数。类型不符、bool、越界或不是 2 的幂时，统一抛出 ValueError，不能自动截断、四舍五入或替换为默认值。

合法示例：64、256、4096。类型非法示例：True、False、256.0、"256"、None。数值非法示例：0、63、100、4097、8192。

## threshold 阈值参数

threshold 默认值为 0.65。可接受 int 或 float，但必须排除 bool；转为浮点数后必须为有限值，且满足 0 到 1，包含两端。不允许 NaN、正负无穷、字符串或 None。合法值返回浮点数，非法值统一抛出 ValueError。

这些参数仅是演示约定。本文没有通用检测阈值公式、检测准确率、真实误报率或跨设备适用性证据。不能根据 threshold=0.65 推导误报率，也不能把该数值称为工程标准或性能保证。

## 演示验收与文件边界

固定测试覆盖 window_size 的 3 个合法输入、5 个类型非法输入、5 个数值非法输入，共 13 个子用例，组织为 3 个 unittest 测试方法。

编码 Agent 仅修改 demo_window_validator.py，实现 validate_window_size；保留 test_demo_window_validator.py 的字节及断言不变。验收命令为 python -m unittest -v test_demo_window_validator。程序测试成功还须由平台独立验收确认，不能仅依据 Agent 的完成描述。
'''


def fixture_source(workspace: str, owner_id: str, reset: bool) -> str:
    """Remote script validates all paths before writing known fixture files."""
    return f'''import hashlib, json, pathlib, subprocess, sys
root = pathlib.Path({workspace!r}).resolve(strict=True)
p = root / {DIRECTORY!r}
assert p.parent == root and p.name == {DIRECTORY!r}
assert not p.is_symlink(), 'demo directory must not be a symlink'
marker = p / {MARKER!r}
files = {{'demo_window_validator.py': {IMPLEMENTATION!r}, 'test_demo_window_validator.py': {TESTS!r}}}
created = not p.exists()
if created:
    p.mkdir()
else:
    assert p.is_dir() and p.resolve(strict=True).parent == root, 'invalid demo directory'
    assert marker.is_file() and not marker.is_symlink(), 'existing directory is not this marked demo fixture'
    saved = json.loads(marker.read_text(encoding='utf-8'))
    assert saved.get('fixture_id') == {FIXTURE_ID!r} and saved.get('owner_id') == {owner_id!r}, 'fixture ownership mismatch'
for name in [*files, {MARKER!r}]:
    target = p / name
    assert target.parent == p and not target.is_symlink(), 'fixture file must not be a symlink'
    assert not target.exists() or target.is_file(), 'fixture target is not a regular file'
if created or {bool(reset)!r}:
    for name, text in files.items():
        (p / name).write_bytes(text.encode('utf-8'))
    marker.write_text(json.dumps({{'fixture_id': {FIXTURE_ID!r}, 'owner_id': {owner_id!r},
        'owned_files': list(files), 'tests_sha256': hashlib.sha256(files['test_demo_window_validator.py'].encode()).hexdigest()}}), encoding='utf-8')
assert all((p / name).is_file() for name in files), 'fixture files are missing; run --reset to restore owned files'
tests_sha = hashlib.sha256((p / 'test_demo_window_validator.py').read_bytes()).hexdigest()
assert tests_sha == hashlib.sha256({TESTS!r}.encode()).hexdigest(), 'fixed tests changed; run --reset to restore fixture'
run = subprocess.run([sys.executable, '-m', 'unittest', '-v', 'test_demo_window_validator'], cwd=p,
    capture_output=True, text=True, timeout=30)
output = (run.stdout + run.stderr).replace(str(p), 'demo-live').replace(str(root), '[workspace]')
assert 'Ran 3 tests' in output, 'expected exactly three fixed unittest methods'
if created or {bool(reset)!r}:
    assert run.returncode != 0 and 'FAILED' in output, 'broken fixture must genuinely fail before the demo'
print(json.dumps({{'status': 'prepared', 'workspace_relative': {DIRECTORY!r}, 'created': created,
    'reset': {bool(reset)!r}, 'fixed_test_methods': 3, 'fixed_subcases': 13,
    'initial_exit_code': run.returncode, 'tests_sha256': tests_sha,
    'source_sha256': hashlib.sha256((p / 'demo_window_validator.py').read_bytes()).hexdigest(),
    'reset_required': run.returncode == 0, 'initial_test_output': output}}, ensure_ascii=False))
'''


async def terminal_context():
    from starlette.requests import Request
    from open_webui.main import app
    from open_webui.models.users import Users
    from open_webui.models.tools import Tools
    from open_webui.utils.tools import get_terminal_tools
    owner = os.environ.get("OPEN_TERMINAL_OWNER_NAME", "wenguang.wu")
    page = await Users.get_users(limit=1000)
    matches = [user for user in page["users"] if user.name == owner]
    if len(matches) != 1:
        raise RuntimeError("需要且只能有一个与配置匹配的 OpenTerminal 用户。")
    user = matches[0]
    request = Request({"type": "http", "app": app, "method": "GET", "path": "/", "headers": [],
                       "query_string": b"", "server": ("demo-helper", 80), "client": ("demo-helper", 12345), "scheme": "http"})
    valves = await Tools.get_tool_valves_by_id("coding_agent") or {}
    metadata = {"chat_id": "demo-prepare-" + uuid.uuid4().hex, "message_id": uuid.uuid4().hex}
    value = await get_terminal_tools(request, valves.get("TERMINAL_ID", "local-open-terminal"), user,
                                     {"__user__": user.model_dump(), "__metadata__": metadata})
    terminal = value[0] if isinstance(value, tuple) else value
    match = re.search(r"current working directory is:\s*([^\r\n]+)", terminal["run_command"]["spec"].get("description", ""), re.I)
    if not match:
        raise RuntimeError("无法确认当前认证用户的终端工作区。")
    workspace = match.group(1).strip()
    if not PurePosixPath(workspace).is_absolute() or ".." in PurePosixPath(workspace).parts:
        raise RuntimeError("授权终端工作区路径无效。")
    return user, terminal, workspace


async def index_document(content: bytes) -> dict:
    import httpx
    token = os.environ.get("RAGFLOW_MCP_TOKEN", "")
    if not token:
        raise RuntimeError("知识库认证配置缺失。")
    digest = hashlib.sha256(content).hexdigest()
    async with httpx.AsyncClient(base_url="http://retrieval-lab:8000", timeout=610,
                                 headers={"Authorization": "Bearer " + token}) as client:
        response = await client.get("/api/overview")
        response.raise_for_status()
        matching = next((doc for doc in response.json().get("documents", []) if doc.get("sha") == digest), None)
        reused = matching is not None
        if matching is None:
            response = await client.post("/api/documents", files={"file": ("DEMO-PARAM-001.md", content, "text/markdown")})
            response.raise_for_status()
            matching = response.json()
        identifier = matching["id"]
        already_indexed = matching.get("status") == "ready" and matching.get("current_indexed_chunks") == matching.get("chunk_count")
        if not already_indexed:
            response = await client.post(f"/api/documents/{identifier}/index")
            response.raise_for_status()
            if response.json().get("status") != "ready":
                raise RuntimeError("演示文档未完成索引。")
        return {"status": "ready", "filename": matching.get("filename", "DEMO-PARAM-001.md"),
                "document_id": identifier, "sha256": digest, "reused_document": reused,
                "reused_index": already_indexed, "fictional": True}


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reset", action="store_true", help="仅恢复已标记 demo-live 的两个固定文件。")
    parser.add_argument("--skip-knowledge", action="store_true", help="只准备真实失败 fixture，不上传或索引。")
    parser.add_argument("--document", type=Path, help="显式演示 Markdown 来源；默认使用仓库文档或完全一致的内置内容。")
    args = parser.parse_args()
    from claude_agent_pipe import _unwrap_owui_tool_result
    user, terminal, workspace = await terminal_context()
    result = _unwrap_owui_tool_result(await terminal["run_command"]["callable"](
        command="python -c " + shlex.quote(fixture_source(workspace, user.id, args.reset)), cwd=workspace, wait="10"))
    if not isinstance(result, dict) or result.get("exit_code") != 0:
        raise RuntimeError("演示目录准备失败；没有修改其他项目。请核对标记和固定测试。")
    output = result.get("output", [])
    text = output if isinstance(output, str) else "".join(part.get("data", part.get("text", "")) for part in output if isinstance(part, dict))
    report = json.loads(text.strip())
    if not args.skip_knowledge:
        source = args.document or Path(__file__).resolve().parent.parent / "docs" / "fixtures" / "demo-param-001.md"
        content = source.read_bytes() if source.is_file() else DOCUMENT.encode("utf-8")
        if args.document and not args.document.is_file():
            raise RuntimeError("指定的演示文档不存在。")
        report["knowledge"] = await index_document(content)
    location = Path("/app/backend/data/demo-live-prepare.json")
    location.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    location.chmod(0o600)
    print(json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
