"""Small, bounded Anthropic-compatible tool-use test against Model Studio.

API keys are accepted through the process environment, never stored here.
Only synthetic prompts/tool data are sent. No actual user files are read.
"""

import json
import os
import time
import urllib.error
import urllib.request


BASE = os.environ.get("BAILIAN_BASE_URL", "https://dashscope.aliyuncs.com/apps/anthropic")
KEY = os.environ["BAILIAN_API_KEY"]
MODELS = os.environ.get("BAILIAN_TEST_MODELS", "qwen-flash,qwen3-coder-flash").split(",")


def call(payload):
    request = urllib.request.Request(
        BASE.rstrip("/") + "/v1/messages",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-api-key": KEY, "anthropic-version": "2023-06-01"},
        method="POST",
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            data = json.load(response)
        return data, round(time.monotonic() - started, 2)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace").replace(KEY, "[REDACTED]")
        print(json.dumps({"model": payload["model"], "http_status": exc.code, "error": body[:800]}, ensure_ascii=False), flush=True)
        return None, round(time.monotonic() - started, 2)


for model in MODELS:
    messages = [{"role": "user", "content": "调用 echo_probe，text 为 TEST_OK。得到工具结果后用一句中文回答。"}]
    payload = {
        "model": model, "max_tokens": 512,
        "system": "你是中文工具助手，需要执行的操作必须真实调用工具，收到结果后直接简短回答。",
        "messages": messages,
        "thinking": {"type": "disabled"},
        "tools": [{"name": "echo_probe", "description": "返回给定文字。",
                   "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}}],
    }
    response, elapsed = call(payload)
    if response is None:
        continue
    blocks = response.get("content", [])
    tools = [b for b in blocks if b.get("type") == "tool_use"]
    print(json.dumps({"model": model, "phase": "select_tool", "seconds": elapsed,
                      "stop_reason": response.get("stop_reason"), "tool_calls": len(tools),
                      "usage": response.get("usage")}, ensure_ascii=False), flush=True)
    if not tools:
        continue
    if any(b.get("name") != "echo_probe" or b.get("input", {}).get("text") != "TEST_OK" for b in tools):
        print("INVALID_TOOL_ARGUMENTS", model, flush=True)
        continue
    messages.append({"role": "assistant", "content": blocks})
    messages.append({"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": b["id"], "content": "TEST_OK"} for b in tools
    ]})
    final, reply_elapsed = call(payload)
    if final:
        answer = "".join(b.get("text", "") for b in final.get("content", []) if b.get("type") == "text")
        print(json.dumps({"model": model, "phase": "answer", "seconds": reply_elapsed,
                          "stop_reason": final.get("stop_reason"), "answer": answer,
                          "usage": final.get("usage")}, ensure_ascii=False), flush=True)
