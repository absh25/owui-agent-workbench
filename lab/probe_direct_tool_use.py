"""Probe the upstream model's native Anthropic tool-use behavior."""

import asyncio
import os

from anthropic import AsyncAnthropic


async def main() -> None:
    client = AsyncAnthropic(
        api_key=os.environ["ANTHROPIC_API_KEY"],
        base_url=os.environ["ANTHROPIC_BASE_URL"],
    )
    response = await client.messages.create(
        model=os.environ["ANTHROPIC_MODEL"],
        max_tokens=512,
        system="你必须始终使用简体中文回答。需要执行命令时必须调用工具，不能声称已经执行。",
        messages=[
            {
                "role": "user",
                "content": "调用 run_command 执行 printf OPEN_TERMINAL_AGENT_OK，然后说明结果。",
            }
        ],
        tools=[
            {
                "name": "run_command",
                "description": "在隔离的 OpenTerminal 沙箱中运行命令。",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string"},
                        "cwd": {"type": "string"},
                    },
                    "required": ["command"],
                },
            }
        ],
    )
    print("STOP_REASON", response.stop_reason)
    print(
        "BLOCKS",
        [
            {
                "type": block.type,
                "name": getattr(block, "name", None),
                "input": getattr(block, "input", None),
                "text": getattr(block, "text", None),
            }
            for block in response.content
        ],
    )
    if response.stop_reason != "tool_use":
        raise RuntimeError("The upstream model did not emit a native tool call")


if __name__ == "__main__":
    asyncio.run(main())
