"""End-to-end: Open WebUI terminal tool -> Agent bridge -> OpenTerminal."""

import asyncio
import sys
import json
import sqlite3
import uuid
import shlex
import os
import re
from unittest.mock import patch

from starlette.requests import Request

from open_webui.main import app
from open_webui.models.users import Users
from open_webui.utils.tools import get_terminal_tools
from open_webui.utils.plugin import load_function_module_by_id


async def verify_sandbox_bridge(module, tools, workspace):
    """Use real OWUI callables to check the deployed adapter without model claims."""
    handlers = {}
    original_tool = module.tool

    def capture_tool(name, description, schema):
        def decorate(handler):
            handlers[name] = handler
            return original_tool(name, description, schema)(handler)
        return decorate

    with patch.object(module, "tool", capture_tool):
        module._build_owui_mcp_server(tools)

    async def call(name, **arguments):
        result = await handlers[name](arguments)
        text = result["content"][0]["text"]
        if "CIMultiDictProxy" in text:
            raise RuntimeError("Transport headers leaked into Agent evidence")
        return result, text

    identifier = uuid.uuid4().hex[:10]
    path = f"{workspace}/sandbox-check-{identifier}.txt"
    marker = f"SANDBOX_ROUNDTRIP_{identifier}"
    written, _ = await call("write_file", path=path, content=marker)
    if written.get("isError"):
        raise RuntimeError(f"OWUI sandbox write failed: {written}")
    read, text = await call("read_file", path=path)
    if read.get("isError") or marker not in text:
        raise RuntimeError(f"OWUI sandbox read failed: {read}")
    cwd, text = await call("run_command", command="pwd", cwd="/app/backend")
    if cwd.get("isError") or workspace not in text:
        raise RuntimeError(f"Backend cwd was not corrected to sandbox cwd: {cwd}")
    failed, text = await call("run_command", command="python -c 'raise SystemExit(7)'", wait="10")
    if not failed.get("isError") or json.loads(text).get("exit_code") != 7:
        raise RuntimeError(f"Nonzero exit was not returned as Agent error: {failed}")
    missing, _ = await call("list_files", directory=f"{workspace}/missing-{identifier}")
    if not missing.get("isError"):
        raise RuntimeError(f"Missing directory was not returned as Agent error: {missing}")
    pending, text = await call("run_command",
        command="python -c 'import time; time.sleep(1.5); print(\"ASYNC_SANDBOX_OK\")'", wait="0")
    process = json.loads(text)
    if pending.get("isError") or not process.get("id"):
        raise RuntimeError(f"Background process did not start: {pending}")
    properties = tools["get_process_status"]["spec"]["parameters"]["properties"]
    id_parameter = next((key for key in ("command_id", "process_id", "id") if key in properties), None)
    if not id_parameter:
        raise RuntimeError(f"Unknown status tool schema: {list(properties)}")
    for _ in range(20):
        status, text = await call("get_process_status", **{id_parameter: process["id"]})
        result = json.loads(text)
        if result.get("exit_code") is not None:
            if status.get("isError") or result["exit_code"] != 0 or "ASYNC_SANDBOX_OK" not in text:
                raise RuntimeError(f"Background command verification failed: {status}")
            break
        await asyncio.sleep(0.25)
    else:
        raise RuntimeError("Background command did not finish")
    print("SANDBOX_BRIDGE_OK", json.dumps({
        "workspace": workspace, "read_write_file": path, "backend_cwd_corrected": True,
        "nonzero_exit_is_error": True, "missing_directory_is_error": True,
        "background_process_verified": True,
    }), flush=True)


async def main() -> None:
    page = await Users.get_users(limit=1000)
    users = page.get("users", []) if isinstance(page, dict) else []
    owner_name = os.environ.get("OPEN_TERMINAL_OWNER_NAME", "wenguang.wu")
    user = next((item for item in users if item.name == owner_name), None)
    if user is None:
        raise RuntimeError(f"OpenTerminal owner {owner_name!r} does not exist in OWUI")

    request = Request(
        {
            "type": "http",
            "app": app,
            "method": "GET",
            "path": "/",
            "headers": [],
            "query_string": b"",
            "server": ("test", 80),
            "client": ("test", 12345),
            "scheme": "http",
        }
    )
    metadata = {
        "chat_id": "terminal-agent-smoke-test",
        "session_id": "terminal-agent-smoke-test",
    }
    tools, _system_prompt = await get_terminal_tools(
        request,
        "local-open-terminal",
        user,
        {"__metadata__": metadata},
    )
    run_command = tools.get("run_command")
    get_process_status = tools.get("get_process_status")
    if not run_command or run_command.get("type") != "terminal":
        raise RuntimeError("Open WebUI did not construct the terminal run_command tool")
    if not get_process_status or get_process_status.get("type") != "terminal":
        raise RuntimeError("Open WebUI did not construct get_process_status")
    print("RUN_COMMAND_SPEC", run_command.get("spec"))
    if "--inspect" in sys.argv:
        return

    # Load exactly the Function saved in OWUI, using OWUI's own loader.
    pipe, _, _ = await load_function_module_by_id("claude_code")
    pipe_module = sys.modules[type(pipe).__module__]
    print("FUNCTION_SOURCE", "OWUI saved claude_code", flush=True)
    match = re.search(r"current working directory is:\s*([^\r\n]+)",
                      run_command["spec"]["description"], re.I)
    if not match:
        raise RuntimeError("OWUI terminal tools did not provide the sandbox workspace")
    workspace = match.group(1).strip()
    if "--sandbox-checks" in sys.argv:
        await verify_sandbox_bridge(pipe_module, tools, workspace)
        return

    calls = []
    raw_results = []
    original_callable = run_command["callable"]

    async def counted_run_command(**kwargs):
        calls.append(kwargs)
        result = await original_callable(**kwargs)
        raw_results.append(result)
        return result

    run_command = dict(run_command)
    run_command["callable"] = counted_run_command

    status_calls = []
    status_results = []
    original_status = get_process_status["callable"]

    async def counted_get_process_status(**kwargs):
        status_calls.append(kwargs)
        result = await original_status(**kwargs)
        status_results.append(result)
        return result

    get_process_status = dict(get_process_status)
    get_process_status["callable"] = counted_get_process_status

    with sqlite3.connect("/app/backend/data/webui.db") as connection:
        saved = connection.execute('SELECT valves FROM "function" WHERE id = ?', ("claude_code",)).fetchone()
    if saved:
        pipe.valves = pipe.Valves(**json.loads(saved[0] or "{}"))
    pipe.valves.MAX_TURNS = 8
    roundtrip = "--file-roundtrip" in sys.argv or "--penguin" in sys.argv
    filename = f"terminal-repair-{uuid.uuid4().hex[:10]}.html"
    if roundtrip:
        tools = dict(tools)
        for tool_name, entry in list(tools.items()):
            original = entry.get("callable")
            if not callable(original):
                continue
            def wrap(name, function):
                async def counted(**kwargs):
                    calls.append({"tool": name, **kwargs})
                    result = await function(**kwargs)
                    raw_results.append(result)
                    return result
                return counted
            tools[tool_name] = {**entry, "callable": wrap(tool_name, original)}
    print("MODEL", pipe.valves.MODEL, flush=True)
    chunks = []
    async for chunk in pipe.pipe(
        {
            "messages": [
                {
                    "role": "user",
                    "content": (
                        f"帮我创建一个企鹅骑自行车的 HTML 动画，写到当前目录的 {filename}。"
                        "不要覆盖其他文件。写完请执行命令确认文件存在且包含 @keyframes，最后简短用中文说明路径。"
                    ) if "--penguin" in sys.argv else (
                        f"使用 write_file 在当前工作目录创建 {filename}，内容是带 CSS 动画的完整 HTML 页面，"
                        "页面标题与正文均包含 TERMINAL_REPAIR_OK。随后使用 run_command 读取文件，"
                        "验证标记和 @keyframes 均存在。必须真实写入并读取验证，最后用中文报告文件路径与结果。"
                    ) if roundtrip else (
                        "调用一次 run_command 工具执行 pwd，等待命令完成并读取结果；"
                        "成功后用一句中文说明 Agent 当前的工作目录。"
                    ),
                }
            ]
        },
        __chat_id__="terminal-agent-smoke-test",
        __metadata__=metadata,
        __user__=user.model_dump(),
        __tools__=tools if roundtrip else {
            "run_command": run_command,
            "get_process_status": get_process_status,
        },
    ):
        chunks.append(chunk)

    output = "".join(chunks)
    if "--penguin" in sys.argv:
        written_paths = [call.get("path") for call in calls if call.get("tool") == "write_file"]
        if not written_paths:
            raise RuntimeError("No penguin HTML file was written")
        for path in written_paths:
            await tools["run_command"]["callable"](
                command=f"test -s {shlex.quote(path)} && grep -n '@keyframes' {shlex.quote(path)}",
                wait="10",
            )
    print("CALLS", calls)
    print("RAW_RESULTS", [pipe_module._unwrap_owui_tool_result(result) for result in raw_results])
    print("STATUS_CALLS", status_calls)
    print("STATUS_RESULTS", [pipe_module._unwrap_owui_tool_result(result) for result in status_results])
    print("OUTPUT", output)
    if roundtrip:
        if not any(call.get("tool") == "write_file" for call in calls):
            raise RuntimeError("No real write_file execution")
        if not any(call.get("tool") == "run_command" for call in calls):
            raise RuntimeError("No real verification command execution")
        command_results = [result[0] if isinstance(result, tuple) else result
                           for call, result in zip(calls, raw_results) if call.get("tool") == "run_command"]
        if not any(isinstance(result, dict) and result.get("exit_code") == 0 for result in command_results):
            raise RuntimeError("Verification command did not finish successfully")
        if "--penguin" not in sys.argv and (
            "TERMINAL_REPAIR_OK" not in str(raw_results) or "@keyframes" not in str(raw_results)
        ):
            raise RuntimeError("Missing verified animation file content")
        if "No such tool available" in output:
            raise RuntimeError("Model still used an invalid tool name")
        print("FILE_ROUNDTRIP_OK", filename)
        return
    if len(calls) != 1 or calls[0].get("command", "").strip() != "pwd":
        raise RuntimeError(f"Expected exactly one real terminal call, got: {calls!r}")
    try:
        wait_seconds = float(calls[0].get("wait", 0))
    except (TypeError, ValueError):
        wait_seconds = 0
    if wait_seconds <= 0:
        raise RuntimeError(f"Short command did not use synchronous wait: {calls!r}")
    if status_calls:
        raise RuntimeError(f"Synchronous short command was polled unnecessarily: {status_calls!r}")
    if workspace not in str(raw_results):
        raise RuntimeError(f"Inline tool result missed expected workspace: {raw_results!r}")
    if workspace not in output:
        raise RuntimeError("Agent did not explain the actual OpenTerminal cwd")
    print("END_TO_END_OK", True)


if __name__ == "__main__":
    asyncio.run(main())
