"""Exercise the saved coding tool with real OWUI grants, SDK and OpenTerminal."""

import asyncio
import json
import os
import shlex
import uuid

from starlette.requests import Request
from open_webui.main import app
from open_webui.models.users import Users
from open_webui.models.tools import Tools as ToolRegistry
from open_webui.utils.plugin import load_tool_module_by_id
from open_webui.utils.tools import get_terminal_tools
from claude_agent_pipe import _unwrap_owui_tool_result


async def main():
    page = await Users.get_users(limit=1000)
    user = next(u for u in page["users"] if u.name == os.environ.get("OPEN_TERMINAL_OWNER_NAME", "wenguang.wu"))
    request = Request({"type": "http", "app": app, "method": "GET", "path": "/",
                       "headers": [], "query_string": b"", "server": ("test", 80),
                       "client": ("test", 12345), "scheme": "http"})
    metadata = {"chat_id": "supervisor-smoke-" + uuid.uuid4().hex,
                "message_id": uuid.uuid4().hex}
    terminal, _ = await get_terminal_tools(request, "local-open-terminal", user, {"__metadata__": metadata})
    directory = "/workspace/wenguang.wu/supervisor-smoke-" + uuid.uuid4().hex[:10]
    await terminal["run_command"]["callable"](command="mkdir -p " + shlex.quote(directory), wait="10")
    agent, _ = await load_tool_module_by_id("coding_agent")
    agent.valves = agent.Valves(**(await ToolRegistry.get_tool_valves_by_id("coding_agent") or {}))
    async def status(event):
        print("STATUS", event["data"]["description"], flush=True)
    result = await agent.delegate_coding_task(
        task="在空项目中创建 calculator.py：add(a,b) 返回 a+b。创建 test_calculator.py，"
             "用 unittest 验证 add(2,3)==5 和 add(-1,1)==0，运行 python -m unittest -v。"
             "任务很小，直接写这两个文件并运行测试，不做额外环境探索。",
        repo=directory, acceptance="两个测试通过；不提交 git，不 push。",
        __request__=request, __user__=user.model_dump(), __metadata__=metadata,
        __event_emitter__=status)
    print("RESULT", json.dumps(result, ensure_ascii=False), flush=True)
    verification = _unwrap_owui_tool_result(await terminal["run_command"]["callable"](
        command="python -m unittest -v", cwd=directory, wait="10"))
    print("INDEPENDENT_VERIFICATION", json.dumps(verification, ensure_ascii=False), flush=True)
    output = "".join(item.get("data", "") for item in verification.get("output", []))
    if result["status"] != "completed" or verification.get("exit_code") != 0 or "Ran 2 tests" not in output:
        raise RuntimeError("编码委派验证失败。")
    print("CODING_DELEGATION_OK", directory, flush=True)


if __name__ == "__main__":
    asyncio.run(main())
