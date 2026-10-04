"""Compare a synthetic tool task through Messages API and Claude Agent SDK.

The local proxy prints request shape/timing only, never credentials or content.
"""

import asyncio
import os
import time

import aiohttp
from aiohttp import web
from anthropic import AsyncAnthropic
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, ResultMessage, tool, create_sdk_mcp_server


PROMPT = "调用 echo_probe 工具一次，text 为 TEST_OK。工具返回后，用中文简短说明结果。"
SYSTEM = "你是工具助手。必须真正调用工具；得到结果后立即简短回答。使用简体中文。"
SCHEMA = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}


async def main():
    base = os.environ["ANTHROPIC_BASE_URL"].rstrip("/")
    key = os.environ["ANTHROPIC_API_KEY"]
    model = os.environ["ANTHROPIC_MODEL"]
    async with AsyncAnthropic(api_key=key, base_url=base, max_retries=0) as client:
        started = time.monotonic()
        response = await asyncio.wait_for(client.messages.create(
            model=model, max_tokens=1024, system=SYSTEM,
            messages=[{"role": "user", "content": PROMPT}],
            tools=[{"name": "echo_probe", "description": "返回给定文字。", "input_schema": SCHEMA}],
        ), 25)
        print("DIRECT", {"seconds": round(time.monotonic()-started, 2), "stop_reason": response.stop_reason,
                         "blocks": [b.type for b in response.content], "usage": response.usage.model_dump()}, flush=True)

    calls = []

    @tool("echo_probe", "返回给定文字。", SCHEMA)
    async def echo(args):
        calls.append(args)
        print("SDK_TOOL_CALLED", len(calls), flush=True)
        return {"content": [{"type": "text", "text": "TEST_OK"}]}

    started = time.monotonic()
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as upstream:
        async def proxy(request):
            payload = await request.json()
            system = payload.get("system", [])
            print("SDK_REQUEST", {
                "elapsed": round(time.monotonic()-started, 2), "path": request.path,
                "model": payload.get("model"), "max_tokens": payload.get("max_tokens"),
                "thinking": payload.get("thinking"), "stream": payload.get("stream"),
                "system_chars": len(str(system)), "tools": len(payload.get("tools", [])),
                "roles": [m.get("role") for m in payload.get("messages", [])],
            }, flush=True)
            headers = {k: v for k, v in request.headers.items()
                       if k.lower() not in {"host", "content-length", "connection"}}
            async with upstream.post(base + request.path, json=payload, headers=headers) as response:
                output = web.StreamResponse(status=response.status, headers={
                    "Content-Type": response.headers.get("Content-Type", "application/json")
                })
                await output.prepare(request)
                first = True
                async for chunk in response.content.iter_any():
                    if first:
                        print("SDK_FIRST_BYTES", round(time.monotonic()-started, 2), response.status, flush=True)
                        first = False
                    await output.write(chunk)
                await output.write_eof()
                print("SDK_HTTP_DONE", round(time.monotonic()-started, 2), flush=True)
                return output

        app = web.Application()
        app.router.add_post("/{path:.*}", proxy)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        options = ClaudeAgentOptions(
            model=model, system_prompt=SYSTEM, tools=[],
            allowed_tools=["mcp__openwebui__echo_probe"],
            mcp_servers={"openwebui": create_sdk_mcp_server("openwebui", tools=[echo])},
            strict_mcp_config=True, setting_sources=[], max_turns=3,
            include_partial_messages=True,
            env={"ANTHROPIC_BASE_URL": f"http://127.0.0.1:{port}", "ANTHROPIC_API_KEY": key,
                 "IS_SANDBOX": "1", "DISABLE_TELEMETRY": "1"},
        )
        try:
            async with ClaudeSDKClient(options=options) as sdk:
                await sdk.query(PROMPT)
                async with asyncio.timeout(40):
                    async for message in sdk.receive_response():
                        if isinstance(message, ResultMessage):
                            print("SDK_RESULT", {"elapsed": round(time.monotonic()-started, 2),
                                  "subtype": message.subtype, "is_error": message.is_error,
                                  "result_chars": len(message.result or ""), "tool_calls": len(calls)}, flush=True)
        except TimeoutError:
            print("SDK_TIMEOUT", round(time.monotonic()-started, 2), "tool_calls", len(calls), flush=True)
        finally:
            await runner.cleanup()


asyncio.run(main())
