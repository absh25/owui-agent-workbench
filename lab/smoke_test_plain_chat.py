import asyncio
import importlib.util
import time


SPEC = importlib.util.spec_from_file_location("claude_pipe", "/lab/claude_agent_pipe.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


async def main() -> None:
    started = time.monotonic()
    chunks = []
    pipe = MODULE.Pipe()
    pipe.valves.MAX_TURNS = 3
    async for chunk in pipe.pipe(
        {"messages": [{"role": "user", "content": "你好，请只回复：你好"}]},
        __chat_id__="plain-chat-smoke-test",
        __metadata__={"session_id": "plain-chat-smoke-test"},
        __tools__={},
    ):
        chunks.append(chunk)
    output = "".join(chunks)
    print("SECONDS", round(time.monotonic() - started, 2))
    print("OUTPUT", output)
    if "你好" not in output:
        raise SystemExit("plain chat response missing")


asyncio.run(main())
