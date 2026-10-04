import asyncio
import json

from open_webui.utils.mcp.client import MCPClient


async def main() -> None:
    client = MCPClient()
    try:
        await client.connect("http://weather-mcp:8000/mcp")
        specs = await client.list_tool_specs()
        print("TOOLS", [item.get("name") for item in specs])
        result = await client.call_tool("get_current_weather", {"location": "深圳"})
        print("RESULT", json.dumps(result, ensure_ascii=False)[:2000])
    finally:
        await client.disconnect()


asyncio.run(main())
