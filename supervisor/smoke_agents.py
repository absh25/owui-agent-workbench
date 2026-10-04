"""Exercise real OWUI -> retrieval Agent -> coding Agent -> OpenTerminal handoff."""

import asyncio
import json
import os
import re
import shlex
import uuid

import httpx
from starlette.requests import Request
from open_webui.main import app
from open_webui.models.chats import Chats, ChatForm
from open_webui.models.users import Users
from open_webui.models.tools import Tools as ToolRegistry
from open_webui.utils.auth import create_token
from open_webui.utils.plugin import load_tool_module_by_id
from open_webui.utils.tools import get_terminal_tools
from claude_agent_pipe import _unwrap_owui_tool_result


def tool_results(message):
    names = {item.get("call_id") or item.get("id"): item.get("name", "")
             for item in message.get("output", []) if item.get("type") == "function_call"}
    results = {}
    for item in message.get("output", []):
        if item.get("type") != "function_call_output":
            continue
        output = item.get("output", [])
        text = output if isinstance(output, str) else "".join(p.get("text", "") for p in output if isinstance(p, dict))
        identifier = item.get("call_id")
        name = names.get(identifier, "")
        try:
            value = json.loads(text)
        except (ValueError, TypeError):
            value = text
        if name.endswith(("delegate_retrieval_task", "delegate_coding_task")):
            # Normalize known MCP text/content envelopes only for specialist
            # results. Other tools may legitimately return arrays or primitives.
            for _ in range(6):
                if isinstance(value, dict) and "status" in value:
                    break
                if isinstance(value, dict) and "content" in value:
                    value = value["content"]
                elif isinstance(value, list) and value and all(
                        isinstance(part, dict) and part.get("type") in {"text", "input_text"}
                        and isinstance(part.get("text"), str) for part in value):
                    value = "".join(part["text"] for part in value)
                elif isinstance(value, str):
                    try:
                        value = json.loads(value)
                    except ValueError:
                        break
                else:
                    break
            assert isinstance(value, dict) and "status" in value, (
                "Specialist returned an invalid result", name, type(value).__name__)
        results[identifier] = value
    return results


def result_shapes(message, results):
    names = {item.get("call_id") or item.get("id"): item.get("name", "")
             for item in message.get("output", []) if item.get("type") == "function_call"}
    return [{"name": names.get(identifier), "type": type(value).__name__,
             **({"status": value.get("status"), "stop_reason": value.get("stop_reason")}
                if isinstance(value, dict) else {"length": len(value)} if isinstance(value, (list, str)) else {})}
            for identifier, value in results.items()]


def retrieval_tasks(message):
    tasks = []
    for item in message.get("output", []):
        if item.get("type") != "function_call" or not item.get("name", "").endswith("delegate_retrieval_task"):
            continue
        arguments = item.get("arguments", {})
        if isinstance(arguments, str):
            arguments = json.loads(arguments)
        assert isinstance(arguments, dict), "Invalid retrieval delegation arguments"
        # Diagnostic output includes only the synthetic question and acceptance,
        # never transferred context or retrieved document bodies.
        tasks.append({key: re.sub(r"sk-[A-Za-z0-9]+", "[redacted]", str(arguments.get(key, "")))
                      for key in ("question", "acceptance")})
    return tasks


async def saved_chat(user, prompt, model_id="owui-main"):
    chat_id, user_id, message_id = (str(uuid.uuid4()) for _ in range(3))
    session_id = "agent-smoke-" + uuid.uuid4().hex
    user_message = {"id": user_id, "role": "user", "content": prompt, "parentId": None,
                    "childrenIds": [message_id], "models": [model_id]}
    assistant = {"id": message_id, "role": "assistant", "content": "", "parentId": user_id,
                 "childrenIds": [], "model": model_id, "done": False}
    assert await Chats.insert_new_chat(chat_id, user.id, ChatForm(chat={
        "title": "三 Agent 协作集成测试", "models": [model_id],
        "history": {"messages": {user_id: user_message, message_id: assistant}, "currentId": message_id},
        "messages": [user_message, assistant]})) is not None
    try:
        async with httpx.AsyncClient(base_url="http://127.0.0.1:8080", timeout=120,
            headers={"Authorization": "Bearer " + create_token({"id": user.id})}) as client:
            response = await client.get("/api/models")
            response.raise_for_status()
            models = response.json()["data"]
            model = next((m for m in models if m["id"] == model_id), None)
            assert model is not None, "Configured model is not exposed: " + model_id
            assert model["name"] == "协作助手", model["name"]
            assert not any(m["id"] in {"owui_supervisor", "claude_code", "claude_code.claude-code"}
                           for m in models), "Legacy Pipe entry unexpectedly remains"
            assert not model.get("info", {}).get("base_model_id"), "Main entry must directly override its Pipe"
            tool_ids = model.get("info", {}).get("meta", {}).get("toolIds") or []
            assert {"retrieval_agent", "coding_agent"}.issubset(tool_ids), (model_id, tool_ids)
            # Match the selector's complete configured tool set, including Jira/weather.
            assert "server:mcp:company-jira" in tool_ids, (model_id, tool_ids)
            response = await client.post("/api/chat/completions", json={
                "model": model_id, "messages": [{"role": "user", "content": prompt}],
                "chat_id": chat_id, "id": message_id, "session_id": session_id, "stream": True,
                "tool_ids": tool_ids, "background_tasks": {}})
            response.raise_for_status()
        for _ in range(240):
            message = await Chats.get_message_by_id_and_message_id(chat_id, message_id)
            if message and (message.get("error") or (message.get("done") and
                    (message.get("content") or message.get("output")))):
                assert not message.get("error"), message.get("error")
                return message
            await asyncio.sleep(1)
        raise RuntimeError("Native three-Agent flow did not finish within 240 seconds")
    finally:
        await Chats.delete_chat_by_id(chat_id)


async def main():
    page = await Users.get_users(limit=1000)
    user = next(u for u in page["users"] if u.name == os.environ.get("OPEN_TERMINAL_OWNER_NAME", "wenguang.wu"))
    request = Request({"type": "http", "app": app, "method": "GET", "path": "/",
        "headers": [], "query_string": b"", "server": ("test", 80),
        "client": ("test", 12345), "scheme": "http"})
    metadata = {"chat_id": "agent-smoke-" + uuid.uuid4().hex, "message_id": uuid.uuid4().hex}

    # Verify the live Agent correctly reports missing evidence from the demo document.
    retrieval, _ = await load_tool_module_by_id("retrieval_agent")
    retrieval.valves = retrieval.Valves(**(await ToolRegistry.get_tool_valves_by_id("retrieval_agent") or {}))
    insufficient = await retrieval.delegate_retrieval_task(
        question="只在《演示-频谱感知实验.md》中查证 SS-TEST-042 的通用检测阈值计算公式与真实检测性能。",
        acceptance="必须找到通用公式、适用条件和真实检测性能，演示参数不能作为这些证据。",
        __request__=request, __user__=user.model_dump(), __metadata__=metadata)
    assert insufficient["status"] == "insufficient_evidence", insufficient
    assert insufficient["stop_reason"] == "evidence_insufficient", insufficient
    assert insufficient["missing_information"], insufficient
    print("LIVE_INSUFFICIENT_EVIDENCE_OK", json.dumps({"stop_reason": insufficient["stop_reason"],
        "attempts": insufficient["attempts"], "missing_information": insufficient["missing_information"]}, ensure_ascii=False), flush=True)

    stopped = await saved_chat(user,
        "请先委派检索 Agent，仅查《演示-频谱感知实验.md》中 SS-TEST-042 的通用检测阈值计算公式、适用条件和真实检测性能。"
        "只有这三类证据充分时，才可以委派编码 Agent 实现通用算法；证据不足必须明确缺口并停止，不能把演示参数当通用方法。")
    stopped_calls = [i for i in stopped.get("output", []) if i.get("type") == "function_call"]
    stopped_names = [i.get("name", "") for i in stopped_calls]
    assert any(n.endswith("delegate_retrieval_task") for n in stopped_names), stopped_names
    assert not any(n.endswith("delegate_coding_task") for n in stopped_names), stopped_names
    assert not any("jira" in n.casefold() for n in stopped_names), stopped_names
    stopped_results = tool_results(stopped)
    print("STOPPED_TOOL_RESULT_SHAPES", json.dumps(result_shapes(stopped, stopped_results), ensure_ascii=False), flush=True)
    print("STOPPED_RETRIEVAL_TASKS", json.dumps(retrieval_tasks(stopped), ensure_ascii=False), flush=True)
    stopped_retrieval_calls = [call for call in stopped_calls if call.get("name", "").endswith("delegate_retrieval_task")]
    assert all(stopped_results[call.get("call_id") or call.get("id")]["status"] == "insufficient_evidence"
               for call in stopped_retrieval_calls), result_shapes(stopped, stopped_results)
    assert all(stopped_results[call.get("call_id") or call.get("id")].get("original_request_present") is True
               for call in stopped_retrieval_calls), "OWUI did not inject the real current user request"
    print("OWUI_INSUFFICIENT_STOPS_CODING_OK", json.dumps({"tool_calls": stopped_names,
        "answer": stopped.get("content", "")}, ensure_ascii=False), flush=True)

    raw_message = await saved_chat(user,
        "查 ragflow 中 SS-TEST-042 的采样率和阈值并引用原文，不提交。")
    raw_calls = [item for item in raw_message.get("output", []) if item.get("type") == "function_call"]
    raw_names = [call.get("name", "") for call in raw_calls]
    raw_retrieval_calls = [c for c in raw_calls if c.get("name", "").endswith("delegate_retrieval_task")]
    assert raw_retrieval_calls, raw_names
    assert not any("jira" in n.casefold() or n.endswith("delegate_coding_task") for n in raw_names), raw_names
    raw_outputs = tool_results(raw_message)
    raw_retrieved = raw_outputs[raw_retrieval_calls[0].get("call_id") or raw_retrieval_calls[0].get("id")]
    assert raw_retrieved["status"] == "sufficient" and raw_retrieved["evidence"], raw_retrieved
    assert raw_retrieved.get("original_request_present") is True, "Original user request was not preserved"
    quoted = "\n".join(ref.get("quote", "") for ref in raw_retrieved["evidence"])
    assert "0.12" in quoted and re.search(r"2\s*MHz|2[ ,]?000[ ,]?000", quoted, re.IGNORECASE), quoted
    assert all(ref.get("citation") and ref.get("quote") for ref in raw_retrieved["evidence"]), raw_retrieved
    raw_answer = raw_message.get("content", "")
    assert "0.12" in raw_answer and re.search(r"2\s*MHz|2[ ,]?000[ ,]?000", raw_answer, re.IGNORECASE), raw_answer
    assert any(ref["filename"] in raw_answer for ref in raw_retrieved["evidence"]), raw_answer
    print("COLLABORATION_ENTRY_KNOWLEDGE_ROUTING_OK", json.dumps({"model": "owui-main", "tool_calls": raw_names,
        "retrieval_status": raw_retrieved["status"], "answer": raw_answer}, ensure_ascii=False), flush=True)

    terminal, _ = await get_terminal_tools(request, "local-open-terminal", user, {"__metadata__": metadata})
    directory = "/workspace/wenguang.wu/three-agent-smoke-" + uuid.uuid4().hex[:10]
    created = _unwrap_owui_tool_result(await terminal["run_command"]["callable"](
        command="mkdir -p " + shlex.quote(directory), wait="10"))
    assert created.get("exit_code") == 0, created
    prompt = (
        "请按专业 Agent 分工完成这个很小的任务：先委派检索 Agent，查《演示-频谱感知实验.md》中 "
        "SS-TEST-042 的采样率、能量检测阈值和演示限制，取得原文引用。"
        "然后委派编码 Agent，在项目目录 " + directory + " 新建 demo_parameters.py，"
        "保存 SAMPLE_RATE_HZ 和 ENERGY_THRESHOLD 两个常量，以及 IS_DEMO=True；"
        "只是保存该演示的参数，不实现真实检测算法，不把演示数据作为工程标准。"
        "给编码 Agent 的 context 必须包含检索得到的数值、来源文件名和演示限制。"
        "再创建 test_demo_parameters.py，用 unittest 分别验证两个常量与原文参数一致，运行 python -m unittest -v。"
        "直接完成这两个文件和两个测试，不做额外环境探索。不 git commit、不 push、不更新 Jira。最后汇报结果和来源。"
    )
    message = await saved_chat(user, prompt)
    calls = [item for item in message.get("output", []) if item.get("type") == "function_call"]
    names = [call.get("name", "") for call in calls]
    retrieval_calls = [c for c in calls if c.get("name", "").endswith("delegate_retrieval_task")]
    coding_calls = [c for c in calls if c.get("name", "").endswith("delegate_coding_task")]
    assert retrieval_calls and coding_calls, names
    assert not any("jira" in n.casefold() for n in names), names
    assert names.index(retrieval_calls[0]["name"]) < names.index(coding_calls[0]["name"]), names
    assert not any(n.endswith("search_knowledge") or n == "run_command" for n in names), names
    outputs = tool_results(message)
    retrieved = outputs[retrieval_calls[0].get("call_id") or retrieval_calls[0].get("id")]
    coded = outputs[coding_calls[0].get("call_id") or coding_calls[0].get("id")]
    assert retrieved["status"] == "sufficient" and retrieved["evidence"], retrieved
    assert retrieved.get("original_request_present") is True, "Original user request was not preserved"
    assert coded["status"] == "completed", coded
    arguments = coding_calls[0]["arguments"]
    if isinstance(arguments, str):
        arguments = json.loads(arguments)
    context = arguments.get("context", "")
    assert "演示-频谱感知实验.md" in context and "0.12" in context, context
    assert any(s in context for s in ("演示", "虚构")), context
    verification = _unwrap_owui_tool_result(await terminal["run_command"]["callable"](
        command="python -m unittest -v", cwd=directory, wait="10"))
    text = "".join(part.get("data", "") for part in verification.get("output", []))
    count = re.search(r"Ran (\d+) tests?", text)
    assert verification.get("exit_code") == 0 and count and int(count.group(1)) >= 2, verification
    values = _unwrap_owui_tool_result(await terminal["run_command"]["callable"](
        command="python -c 'import demo_parameters as p; assert p.SAMPLE_RATE_HZ==2000000; assert p.ENERGY_THRESHOLD==0.12; assert p.IS_DEMO is True; print(\"PARAMETERS_VERIFIED\")'",
        cwd=directory, wait="10"))
    assert values.get("exit_code") == 0, values
    print("THREE_AGENT_HANDOFF_OK", json.dumps({"tool_calls": names, "retrieval_status": retrieved["status"],
        "retrieval_attempts": retrieved["attempts"], "coding_status": coded["status"],
        "workspace": directory, "verified_tests": int(count.group(1)), "answer": message.get("content", "")}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
