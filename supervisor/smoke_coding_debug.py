"""Prove a real main-Agent -> coding-Agent test/fix/retest cycle.

Run inside OWUI with PYTHONPATH=/lab:/app/backend. Only a unique directory in the
authenticated user's OpenTerminal is written. No Git, Jira, or KB operation
is part of this smoke; saved_chat removes its own temporary chat on exit.
The fixture directory is deliberately retained for inspection.
"""

import ast
import asyncio
import hashlib
import json
import os
from pathlib import PurePosixPath
import re
import shlex
import uuid

from starlette.requests import Request
from open_webui.main import app
from open_webui.models.functions import Functions
from open_webui.models.tools import Tools as ToolRegistry
from open_webui.models.users import Users
from open_webui.utils.tools import get_terminal_tools

from claude_agent_pipe import _unwrap_owui_tool_result
from supervisor.smoke_agents import saved_chat, tool_results


IMPLEMENTATION = "def add(a, b):\n    return a - b\n"
TESTS = """import unittest
from calculator import add


class AddTests(unittest.TestCase):
    def test_positive_numbers(self):
        self.assertEqual(add(2, 3), 5)

    def test_zero_sum(self):
        self.assertEqual(add(-1, 1), 0)


if __name__ == '__main__':
    unittest.main()
"""


def terminal_text(result):
    output = result.get("output", [])
    if isinstance(output, str):
        return output
    return "".join(str(part.get("data", part.get("text", "")))
                   for part in output if isinstance(part, dict))


def completed_exit_code(value):
    code = value.get("exit_code")
    if code is None:
        return None
    try:
        return int(code)
    except (ValueError, TypeError):
        return None


def unittest_count(text):
    match = re.search(r"Ran (\d+) tests?\b", text)
    return int(match.group(1)) if match else None


def evidence_text(event):
    value = event.get("result", "")
    if isinstance(value, dict):
        return terminal_text(value)
    if isinstance(value, str):
        # coding_tool records str(payload), so newlines inside output text are
        # escaped in Python repr. Parse before matching test result boundaries.
        for decoder in (json.loads, ast.literal_eval):
            try:
                decoded = decoder(value)
            except (ValueError, SyntaxError, TypeError):
                continue
            if isinstance(decoded, dict):
                return terminal_text(decoded)
        return value
    return str(value)


def is_unittest_command(command, directory):
    try:
        parts = shlex.split(command)
    except ValueError:
        return False
    if parts[:3] == ["cd", directory, "&&"]:
        parts = parts[3:]
    return (len(parts) >= 3 and PurePosixPath(parts[0]).name in {"python", "python3"}
            and parts[1:3] == ["-m", "unittest"]
            and not any(part in {";", "&&", "||", "|"} for part in parts[3:]))


def verify_debug_evidence(evidence, directory):
    """Require observed test failure, a file edit, then observed passing tests."""
    assert isinstance(evidence, list), "Coding Agent omitted its tool evidence"
    failed, edits, passed = [], [], []
    for index, event in enumerate(evidence):
        arguments = event.get("arguments") or {}
        tool = event.get("tool")
        if tool == "run_command":
            assert arguments.get("cwd") == directory, "A coding command ran outside the requested fixture cwd"
            assert not re.search(r"\bgit\s|\bjira\b", str(arguments.get("command", "")), re.I), (
                "An unrelated Git/Jira command appeared in the coding evidence")
        if tool == "run_command" and is_unittest_command(arguments.get("command", ""), directory):
            code, output = completed_exit_code(event), evidence_text(event)
            if code not in (None, 0) and unittest_count(output) == 2 and "AssertionError" in output:
                failed.append(index)
            if code == 0 and unittest_count(output) == 2 and re.search(r"\bOK\b", output):
                passed.append(index)
        if tool in {"write_file", "replace_in_file", "apply_patch"} and not event.get("is_error"):
            path = arguments.get("path")
            identifies_source = path in {"calculator.py", directory + "/calculator.py"}
            if tool == "apply_patch" and path is None:
                # Patch content is intentionally excluded from coding_tool's
                # evidence. Its tool result can still identify the edited file.
                identifies_source = "calculator.py" in str(event.get("result", ""))
            if identifies_source:
                edits.append(index)
    sequence = next(((failure, edit, success) for failure in failed for edit in edits
                     for success in passed if failure < edit < success), None)
    assert sequence is not None, (
        "No observed two-test failure -> calculator.py edit -> two-test success sequence; "
        f"failure_indexes={failed}, edit_indexes={edits}, success_indexes={passed}")
    return {"failed_test_event": sequence[0],
            "failed_test_exit_code": completed_exit_code(evidence[sequence[0]]),
            "source_edit_event": sequence[1], "source_edit_tool": evidence[sequence[1]]["tool"],
            "passed_test_event": sequence[2],
            "passed_test_exit_code": completed_exit_code(evidence[sequence[2]]),
            "tool_calls": len(evidence)}


async def main():
    users = await Users.get_users(limit=1000)
    owner = os.environ.get("OPEN_TERMINAL_OWNER_NAME", "wenguang.wu")
    user = next((candidate for candidate in users["users"] if candidate.name == owner), None)
    assert user is not None, "Configured OpenTerminal owner was not found"
    request = Request({"type": "http", "app": app, "method": "GET", "path": "/",
        "headers": [], "query_string": b"", "server": ("test", 80),
        "client": ("test", 12345), "scheme": "http"})
    metadata = {"chat_id": "coding-debug-smoke-" + uuid.uuid4().hex,
                "message_id": uuid.uuid4().hex}
    terminal_result = await get_terminal_tools(request, "local-open-terminal", user,
                                              {"__metadata__": metadata})
    terminal = terminal_result[0] if isinstance(terminal_result, tuple) else terminal_result
    description = terminal["run_command"]["spec"].get("description", "")
    match = re.search(r"current working directory is:\s*([^\r\n]+)", description, re.I)
    assert match, "Could not establish the authorized terminal workspace"
    workspace = match.group(1).strip()
    assert workspace == "/workspace/wenguang.wu", "Unexpected terminal workspace; no fixture was written"
    directory = str(PurePosixPath(workspace) / ("debug-smoke-" + uuid.uuid4().hex[:12]))

    async def command(source, cwd=workspace):
        result = _unwrap_owui_tool_result(await terminal["run_command"]["callable"](
            command=source, cwd=cwd, wait="10"))
        assert isinstance(result, dict), "Terminal returned an invalid command result"
        assert completed_exit_code(result) == 0, "Independent terminal verification failed"
        return terminal_text(result)

    prepare = ("import pathlib,json,hashlib; p=pathlib.Path(" + repr(directory) + "); "
               "p.mkdir(exist_ok=False); "
               "(p/'calculator.py').write_text(" + repr(IMPLEMENTATION) + ",encoding='utf-8'); "
               "(p/'test_calculator.py').write_text(" + repr(TESTS) + ",encoding='utf-8'); "
               "print(json.dumps({'tests_sha256':hashlib.sha256((p/'test_calculator.py').read_bytes()).hexdigest(),"
               "'source_sha256':hashlib.sha256((p/'calculator.py').read_bytes()).hexdigest()}))")
    initial = json.loads((await command("python -c " + shlex.quote(prepare))).strip())
    assert initial["tests_sha256"] == hashlib.sha256(TESTS.encode()).hexdigest(), "Fixture test bytes changed"
    prompt = (
        "请委派编码 Agent 完成一次真实的小型调试任务，只委派一次 delegate_coding_task。"
        "项目目录是 " + directory + "，里面已经有 calculator.py 和 test_calculator.py。"
        "先运行原封不动的命令 python -m unittest -v，读取真实失败和 traceback；"
        "然后定位 add(a,b) 的问题，使用 write_file、replace_in_file 或 apply_patch 仅修改 calculator.py，"
        "再次运行原封不动的命令 python -m unittest -v，直到原有两项测试通过。"
        "不要修改、替换或删除 test_calculator.py，不要增加测试或改变测试断言。"
        "没有知识检索需求，不调用检索 Agent、Jira、天气或其他业务工具。"
        "不要 git commit、push 或发布；仅执行上述调试并返回真实的失败、修改和重跑结果。"
        "给编码 Agent 的 task/acceptance 必须保留先跑失败、仅修实现、保留测试、重新运行这四项要求。"
    )
    print("CODING_DEBUG_SMOKE_STARTED", json.dumps({"workspace": directory}, ensure_ascii=False), flush=True)
    message = await saved_chat(user, prompt)
    calls = [event for event in message.get("output", []) if event.get("type") == "function_call"]
    assert len(calls) == 1 and calls[0].get("name", "").endswith("delegate_coding_task"), (
        "Expected exactly one coding delegation and no other main-Agent tool calls")
    call = calls[0]
    result = tool_results(message).get(call.get("call_id") or call.get("id"))
    assert isinstance(result, dict) and result.get("status") == "completed", "Coding Agent did not complete"
    evidence = result.get("tool_evidence") or []
    print("CODING_DEBUG_TOOL_DIAGNOSTICS", json.dumps([{
        "index": index, "tool": event.get("tool"), "exit_code": completed_exit_code(event),
        "unittest_command": is_unittest_command((event.get("arguments") or {}).get("command", ""), directory),
        "reported_test_count": unittest_count(evidence_text(event)),
        "assertion_failure": "AssertionError" in evidence_text(event),
        "reports_ok": bool(re.search(r"\bOK\b", evidence_text(event))),
        "source_path": (event.get("arguments") or {}).get("path") in {"calculator.py", directory + "/calculator.py"},
        "is_error": event.get("is_error")
    } for index, event in enumerate(evidence)], ensure_ascii=False), flush=True)
    sequence = verify_debug_evidence(evidence, directory)

    verification_output = await command("python -m unittest -v", cwd=directory)
    assert unittest_count(verification_output) == 2 and re.search(r"\bOK\b", verification_output), (
        "Independent rerun did not prove that the original two tests passed")
    verification = ("import pathlib,json,hashlib; import calculator; "
        "assert calculator.add(2,3)==5 and calculator.add(-1,1)==0; "
        "p=pathlib.Path('.'); "
        "print(json.dumps({'tests_sha256':hashlib.sha256((p/'test_calculator.py').read_bytes()).hexdigest(),"
        "'source_sha256':hashlib.sha256((p/'calculator.py').read_bytes()).hexdigest()}))")
    final = json.loads((await command("python -c " + shlex.quote(verification), cwd=directory)).strip())
    assert initial["tests_sha256"] == final["tests_sha256"], "Coding Agent altered the fixed tests"
    assert initial["source_sha256"] != final["source_sha256"], "Implementation was not changed"
    main_valves = await Functions.get_function_valves_by_id("owui-main") or {}
    coding_valves = await ToolRegistry.get_tool_valves_by_id("coding_agent") or {}
    print("CODING_DEBUG_SMOKE_OK", json.dumps({"status": "passed", "workspace": directory,
        "main_model": main_valves.get("MODEL"), "coding_model": coding_valves.get("MODEL"),
        "main_tool_calls": [call["name"]], "coding_status": result["status"],
        "debug_sequence": sequence, "changed_files": ["calculator.py"],
        "tests_unchanged": True, "tests_sha256": final["tests_sha256"],
        "independently_passed_tests": 2, "no_git_or_jira_requested": True}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
