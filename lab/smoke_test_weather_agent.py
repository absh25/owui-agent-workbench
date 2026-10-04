"""Full Claude Agent -> OWUI callable -> weather MCP smoke test."""

import asyncio
import importlib.util
import time

from open_webui.utils.mcp.client import MCPClient


SPEC = importlib.util.spec_from_file_location("claude_pipe", "/lab/claude_agent_pipe.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


async def main() -> None:
    started = time.monotonic()
    events = []

    async def emit(event):
        if event.get("type") == "status":
            events.append(event["data"])
            print(
                "STATUS", round(time.monotonic() - started, 1),
                event["data"]["description"], flush=True,
            )

    discovery = MCPClient()
    await discovery.connect("http://weather-mcp:8000/mcp")
    specs = await discovery.list_tool_specs()
    await discovery.disconnect()
    current_spec = next(item for item in specs if item["name"] == "get_current_weather")
    calls = []

    async def call_weather(**kwargs):
        calls.append(kwargs)
        client = MCPClient()
        try:
            await client.connect("http://weather-mcp:8000/mcp")
            return await client.call_tool("get_current_weather", kwargs)
        finally:
            await client.disconnect()

    exposed_name = "local-weather_get_current_weather"
    tools = {
        exposed_name: {
            "callable": call_weather,
            "spec": {**current_spec, "name": exposed_name},
            "type": "mcp",
            "direct": False,
        }
    }
    pipe = MODULE.Pipe()
    pipe.valves.TOOL_MODE = "owui_only"
    pipe.valves.MAX_TURNS = 5
    chunks = []
    async for chunk in pipe.pipe(
        {
            "messages": [
                {
                    "role": "user",
                    "content": "必须调用天气工具查询深圳当前天气，并用一句中文回答。",
                }
            ]
        },
        __chat_id__="weather-agent-smoke-test",
        __metadata__={"session_id": "weather-agent-smoke-test"},
        __tools__=tools,
        __event_emitter__=emit,
    ):
        chunks.append(chunk)
        print("CHUNK", round(time.monotonic() - started, 1), repr(chunk[:100]), flush=True)

    output = "".join(chunks)
    print("CALLS", calls)
    print("OUTPUT", output)
    if not calls or not any(
        city in calls[0].get("location", "").lower() for city in ("深圳", "shenzhen")
    ):
        raise SystemExit(f"weather tool was not called correctly: {calls!r}")
    if "深圳" not in output:
        raise SystemExit("final answer did not mention 深圳")
    if not events or not events[-1]["done"]:
        raise SystemExit("final progress event was not emitted")


asyncio.run(main())
