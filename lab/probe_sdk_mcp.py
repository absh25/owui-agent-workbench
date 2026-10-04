"""Verify Claude Agent SDK can expose and execute an in-process MCP tool."""

import asyncio

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    SystemMessage,
    ToolUseBlock,
    create_sdk_mcp_server,
    tool,
)


CALLS = []


@tool(
    "probe_pwd",
    "Return the current working directory. You must use this for a cwd request.",
    {"type": "object", "properties": {}, "additionalProperties": False},
)
async def probe_pwd(args):
    print("HANDLER_START", args, flush=True)
    CALLS.append(args)
    response = {"content": [{"type": "text", "text": "/workspace/probe"}]}
    print("HANDLER_RETURN", response, flush=True)
    return response


async def main():
    server = create_sdk_mcp_server("openwebui", "1.0", tools=[probe_pwd])
    options = ClaudeAgentOptions(
        model="deepseek-ai/DeepSeek-V4-Flash",
        tools=[],
        allowed_tools=["mcp__openwebui__probe_pwd"],
        disallowed_tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep"],
        mcp_servers={"openwebui": server},
        strict_mcp_config=True,
        max_turns=5,
        system_prompt={
            "type": "preset",
            "preset": "claude_code",
            "append": "必须调用 probe_pwd 工具回答工作目录，不得猜测。",
        },
    )
    result = None
    async with ClaudeSDKClient(options=options) as client:
        await client.query("调用 probe_pwd，返回真实工作目录。")
        async for message in client.receive_response():
            if isinstance(message, SystemMessage) and message.subtype == "init":
                print("INIT_TOOLS", message.data.get("tools"))
            for block in getattr(message, "content", []) or []:
                if isinstance(block, ToolUseBlock):
                    print("TOOL_USE", block.name, block.input)
            if isinstance(message, ResultMessage):
                result = message
                print("RESULT", message.subtype, message.result)
    print("CALLS", CALLS)
    if not CALLS:
        raise RuntimeError("Claude Agent SDK did not execute the in-process MCP tool")
    if result is None or "/workspace/probe" not in str(result.result):
        raise RuntimeError("Agent did not return the MCP tool result")
    print("SDK_MCP_OK", True)


if __name__ == "__main__":
    asyncio.run(main())
