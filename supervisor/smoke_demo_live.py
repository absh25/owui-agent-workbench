"""Rehearse the real configured OWUI main assistant and retain its demo chat.

Use --prompt-file to test a prepared prompt. Results are not pre-recorded or
mocked. Full messages remain private in backend data; stdout contains only
demo task statuses, actual elapsed time and a bounded safe result summary.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import re
import time
import uuid


PROMPT = """请用 execute_task_plan 执行一个真实的三节点任务计划，不要分开顺序调用工具。
A（检索）：委派检索 Agent，只查知识库《DEMO-PARAM-001.md》的 window_size 类型、bool 排除、范围、2 的幂和演示限制，取得原文引用；不是要求真实检测性能。
B（独立业务）：用 Jira 的 get_jira_create_context 只读列出我实际可见的项目及创建上下文，不创建或更新工单。B 无依赖，与 A 同时启动。
C（编码）：依赖 A 成功，委派编码 Agent 在当前终端工作区下已有的 demo-live 目录，仅修复 demo_window_validator.py 的 validate_window_size。不要修改测试文件 test_demo_window_validator.py，保留其原始字节及断言，不新建或删除测试。编码 task 和 acceptance 都必须完整保留“不要修改测试文件 test_demo_window_validator.py”这句话。先运行 python -m unittest -v test_demo_window_validator 读取实际失败，再只修实现，再运行同一命令验证。验收命令 verification_commands=["python -m unittest -v test_demo_window_validator"]，repo="demo-live"，无需网页 preview_target。系统自动把 A 的真实依据加入编码 context。
任务最多一次检索、一次编码；按当前提供工具的实际完整名称构建计划，不要猜工具前缀。B 即使失败也不得阻断 C，因为 C 只依赖 A。请展示实际并发、C 等待 A 的原因、原文引用、测试和验收结果；不声称虚构规范具有真实工程性能。不 git commit、不 push、不发布、不写 Jira。
"""


def _safe(value: str) -> str:
    value = re.sub(r"(?:Bearer\s+|sk-)[A-Za-z0-9._~+/=-]+", "[redacted]", str(value))
    value = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[redacted-email]", value)
    return re.sub(r"/workspace/[^/\s\"']+", "/workspace/[user]", value)


def _parsed_result(value):
    for _ in range(8):
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                break
        elif isinstance(value, dict) and "status" not in value and "content" in value:
            value = value["content"]
        elif isinstance(value, list):
            text = "".join(part.get("text", "") for part in value if isinstance(part, dict) and part.get("type") in {"text", "input_text"})
            if not text:
                break
            value = text
        else:
            break
    return value


def plan_results(message: dict) -> list[dict]:
    names = {item.get("call_id") or item.get("id"): item.get("name", "")
             for item in message.get("output", []) if item.get("type") == "function_call"}
    results = []
    for item in message.get("output", []):
        if item.get("type") != "function_call_output" or not names.get(item.get("call_id"), "").endswith("execute_task_plan"):
            continue
        parsed = _parsed_result(item.get("output", []))
        if isinstance(parsed, dict):
            results.append(parsed)
    return results


def safe_plan_summary(plan: dict) -> dict:
    tasks = []
    for task in plan.get("tasks", []):
        actual = task.get("result")
        actual = actual if isinstance(actual, dict) else {}
        verification = actual.get("verification") or {}
        tasks.append({"id": task.get("id"), "title": _safe(task.get("title", "")), "tool": task.get("tool"),
                      "status": task.get("status"), "depends_on": task.get("depends_on"),
                      "started_after_seconds": task.get("started_after_seconds"), "wall_seconds": task.get("wall_seconds"),
                      "result_status": actual.get("status"), "verification_passed": verification.get("passed"),
                      "evidence_count": len(actual.get("evidence", [])),
                      "checks": [{"kind": check.get("kind"), "exit_code": check.get("exit_code"), "passed": check.get("passed")}
                                 for check in verification.get("checks", [])],
                      "summary": _safe(actual.get("summary", actual.get("error", "")))[:500]})
    return {"status": plan.get("status"), "peak_parallel": plan.get("peak_parallel"),
            "wall_seconds": plan.get("wall_seconds"), "tasks": tasks}


async def verify_fixed_test_sha() -> dict:
    """Read actual terminal bytes and compare them with the preparation record."""
    import hashlib
    import shlex
    from claude_agent_pipe import _unwrap_owui_tool_result
    from supervisor.prepare_demo_live import terminal_context, MARKER, FIXTURE_ID, TESTS
    user, terminal, workspace = await terminal_context()
    saved_path = Path("/app/backend/data/demo-live-prepare.json")
    saved = json.loads(saved_path.read_text(encoding="utf-8")) if saved_path.is_file() else {}
    expected = saved.get("tests_sha256")
    script = f'''import pathlib,json,hashlib
root=pathlib.Path({workspace!r}).resolve(strict=True)
p=root/'demo-live'
assert not p.is_symlink() and p.resolve(strict=True).parent==root
marker=p/{MARKER!r}
test=p/'test_demo_window_validator.py'
assert marker.is_file() and not marker.is_symlink() and test.is_file() and not test.is_symlink()
saved=json.loads(marker.read_text(encoding='utf-8'))
assert saved.get('fixture_id')=={FIXTURE_ID!r} and saved.get('owner_id')=={user.id!r}
print(json.dumps({{'tests_sha256':hashlib.sha256(test.read_bytes()).hexdigest()}}))
'''
    result = _unwrap_owui_tool_result(await terminal["run_command"]["callable"](
        command="python -c " + shlex.quote(script), cwd=workspace, wait="10"))
    if not isinstance(result, dict) or result.get("exit_code") != 0:
        return {"passed": False, "tests_unchanged": False, "reason": "无法独立读取固定测试原件。"}
    output = result.get("output", [])
    text = output if isinstance(output, str) else "".join(part.get("data", part.get("text", "")) for part in output if isinstance(part, dict))
    actual = json.loads(text.strip())["tests_sha256"]
    fixture_sha = hashlib.sha256(TESTS.encode()).hexdigest()
    return {"passed": bool(expected and expected == actual == fixture_sha),
            "tests_unchanged": bool(expected and expected == actual),
            "prepared_tests_sha256": expected, "actual_tests_sha256": actual,
            "fixture_definition_sha256": fixture_sha, "fixed_test_methods": 3, "fixed_subcases": 13,
            "reason": "" if expected else "尚无准备记录，不能声称固定测试原件未改变。"}


async def rehearse(prompt: str, *, title="OWUI 现场演示：并发与依赖等待", delete_chat=False, timeout=660,
                   report_file=Path("/app/backend/data/demo-live-report.json")):
    import httpx
    from open_webui.models.chats import Chats, ChatForm
    from open_webui.models.users import Users
    from open_webui.utils.auth import create_token
    owner = os.environ.get("OPEN_TERMINAL_OWNER_NAME", "wenguang.wu")
    page = await Users.get_users(limit=1000)
    matches = [user for user in page["users"] if user.name == owner]
    if len(matches) != 1:
        raise RuntimeError("无法唯一确定演示用户。")
    user = matches[0]
    chat_id, user_message_id, message_id = (str(uuid.uuid4()) for _ in range(3))
    session_id = "demo-live-" + uuid.uuid4().hex
    user_message = {"id": user_message_id, "role": "user", "content": prompt, "parentId": None,
                    "childrenIds": [message_id], "models": ["owui-main"]}
    assistant = {"id": message_id, "role": "assistant", "content": "", "parentId": user_message_id,
                 "childrenIds": [], "model": "owui-main", "done": False}
    inserted = await Chats.insert_new_chat(chat_id, user.id, ChatForm(chat={
        "title": title, "models": ["owui-main"], "history": {
            "messages": {user_message_id: user_message, message_id: assistant}, "currentId": message_id},
        "messages": [user_message, assistant]}))
    if inserted is None:
        raise RuntimeError("无法保存现场演示聊天。")
    started = time.monotonic()
    first_status = None
    observed_statuses = []
    observed_count = 0
    message = assistant
    limit = max(1, min(float(timeout), 660))
    terminal_status = "failed"
    try:
        async with httpx.AsyncClient(base_url="http://127.0.0.1:8080", timeout=httpx.Timeout(60),
                                     headers={"Authorization": "Bearer " + create_token({"id": user.id})}) as client:
            response = await client.get("/api/models")
            response.raise_for_status()
            model = next((item for item in response.json().get("data", []) if item.get("id") == "owui-main"), None)
            if model is None:
                raise RuntimeError("协作助手模型未向当前用户开放。")
            tool_ids = model.get("info", {}).get("meta", {}).get("toolIds") or []
            if not {"retrieval_agent", "coding_agent"}.issubset(tool_ids):
                raise RuntimeError("协作助手缺少专业委派工具绑定。")
            response = await client.post("/api/chat/completions", json={
                "model": "owui-main", "messages": [{"role": "user", "content": prompt}],
                "chat_id": chat_id, "id": message_id, "user_message": user_message,
                "session_id": session_id, "stream": True,
                "tool_ids": tool_ids, "background_tasks": {}})
            response.raise_for_status()
            print(json.dumps({"stage": "started", "chat_id": chat_id,
                              "url": "http://127.0.0.1:13000/c/" + chat_id}, ensure_ascii=False), flush=True)
            deadline = started + limit
            while time.monotonic() < deadline:
                current = await Chats.get_message_by_id_and_message_id(chat_id, message_id)
                if current:
                    message = current
                    history = message.get("statusHistory") or []
                    if history and first_status is None:
                        first_status = round(time.monotonic() - started, 3)
                    for event in history[observed_count:]:
                        observed_statuses.append({"observed_after_seconds": round(time.monotonic() - started, 3),
                                                  "description": _safe(event.get("description", ""))[:1200],
                                                  "done": bool(event.get("done"))})
                    observed_count = len(history)
                    if message.get("error") or (message.get("done") and (message.get("content") or message.get("output"))):
                        terminal_status = "failed" if message.get("error") else "finished"
                        break
                await asyncio.sleep(min(0.5, max(0, deadline - time.monotonic())))
            else:
                terminal_status = "timed_out"
                # Stop the actual background chat task before retaining its
                # incomplete message. Never leave a timed-out rehearsal running.
                response = await client.post("/api/tasks/chat/" + chat_id + "/stop")
                if response.status_code not in {200, 404}:
                    terminal_status = "timed_out_stop_failed"
        plans = plan_results(message)
        report = {"status": terminal_status, "chat_id": chat_id, "title": title,
                  "elapsed_seconds": round(time.monotonic() - started, 3),
                  "first_status_observed_seconds": first_status, "status_events": observed_statuses,
                  "plans": [safe_plan_summary(plan) for plan in plans],
                  "answer_excerpt": _safe(message.get("content", ""))[:2400],
                  "chat_retained": not delete_chat}
        if any(task.get("tool", "").endswith("delegate_coding_task")
               for plan in plans for task in plan.get("tasks", [])):
            report["fixed_test_integrity"] = await verify_fixed_test_sha()
        destination = Path(report_file)
        if not destination.resolve().is_relative_to(Path("/app/backend/data").resolve()):
            raise RuntimeError("完整演示报告必须保留在私有 backend data 目录内。")
        destination.write_text(json.dumps({**report, "message": message}, ensure_ascii=False, indent=2), encoding="utf-8")
        destination.chmod(0o600)
        return report
    finally:
        if delete_chat:
            await Chats.delete_chat_by_id(chat_id)


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prompt-file", type=Path)
    parser.add_argument("--title", default="OWUI 现场演示：并发与依赖等待")
    parser.add_argument("--delete-chat", action="store_true")
    parser.add_argument("--timeout", type=float, default=660)
    parser.add_argument("--report-file", type=Path, default=Path("/app/backend/data/demo-live-report.json"))
    args = parser.parse_args()
    prompt = args.prompt_file.read_text(encoding="utf-8-sig") if args.prompt_file else PROMPT
    if not prompt.strip():
        raise RuntimeError("演示问题不能为空。")
    print(json.dumps(await rehearse(prompt, title=args.title, delete_chat=args.delete_chat,
                                   timeout=args.timeout, report_file=args.report_file), ensure_ascii=False), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
