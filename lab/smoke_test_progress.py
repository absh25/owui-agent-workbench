"""Verify OWUI receives live progress during a real Claude SDK tool call."""

import asyncio
import importlib.util


SPEC = importlib.util.spec_from_file_location("claude_pipe", "/lab/claude_agent_pipe.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


async def main() -> None:
    events = []
    calls = []

    async def emit(event):
        if event.get("type") == "status":
            events.append(event["data"])
            print("STATUS", event["data"]["description"], flush=True)

    async def echo_owui(text: str) -> dict:
        calls.append(text)
        await asyncio.sleep(6)
        return {"status": "success", "echo": text}

    tools = {
        "echo_owui": {
            "tool_id": "progress-smoke-test",
            "callable": echo_owui,
            "spec": {
                "name": "echo_owui",
                "description": "Echo text through Open WebUI when explicitly requested.",
                "parameters": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            },
        }
    }
    pipe = MODULE.Pipe()
    pipe.valves.TOOL_MODE = "owui_only"
    pipe.valves.MAX_TURNS = 5
    pipe.valves.MODEL_SILENCE_TIMEOUT_SECONDS = 30
    chunks = []
    async for chunk in pipe.pipe(
        {
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "调用 echo_owui 工具一次，text 设为 PROGRESS_OK；"
                        "等工具完成后回复 PROGRESS_OK。"
                    ),
                }
            ]
        },
        __chat_id__="progress-smoke-test",
        __metadata__={"session_id": "progress-smoke-test"},
        __tools__=tools,
        __event_emitter__=emit,
    ):
        chunks.append(chunk)

    descriptions = [event["description"] for event in events]
    output = "".join(chunks)
    print("CALLS", calls)
    print("OUTPUT_TAIL", output[-500:])
    assert calls == ["PROGRESS_OK"], calls
    assert any("正在等待模型" in text for text in descriptions), descriptions
    assert any("正在调用工具" in text for text in descriptions), descriptions
    assert any("⏳" in text for text in descriptions), descriptions
    assert any("工具完成" in text for text in descriptions), descriptions
    assert events[-1] == {"description": "已完成。", "done": True}, events[-1]

    plain_events = []

    async def emit_plain(event):
        if event.get("type") == "status":
            plain_events.append(event["data"])
            print("PLAIN_STATUS", event["data"]["description"], flush=True)

    plain_pipe = MODULE.Pipe()
    plain_chunks = []
    async for chunk in plain_pipe.pipe(
        {"messages": [{"role": "user", "content": "你好"}]},
        __chat_id__="progress-plain-smoke-test",
        __metadata__={"session_id": "progress-plain-smoke-test"},
        __tools__={},
        __event_emitter__=emit_plain,
    ):
        plain_chunks.append(chunk)
    print("PLAIN_OUTPUT", "".join(plain_chunks))
    assert plain_events[0]["description"] == "正在连接模型……", plain_events
    assert any("正在等待模型响应" in item["description"] for item in plain_events)
    assert plain_events[-1]["done"] is True, plain_events
    print("PROGRESS_OK", True)


if __name__ == "__main__":
    asyncio.run(main())
