"""End-to-end smoke test for the request-scoped Open WebUI tool bridge."""

import asyncio
import importlib.util


SPEC = importlib.util.spec_from_file_location("claude_pipe", "/lab/claude_agent_pipe.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


async def main() -> None:
    calls = []

    async def echo_owui(text: str) -> dict:
        calls.append(text)
        return {"status": "success", "echo": text}

    tools = {
        "echo_owui": {
            "tool_id": "smoke-test",
            "callable": echo_owui,
            "spec": {
                "name": "echo_owui",
                "description": "Echo text through Open WebUI. Always use when explicitly requested.",
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
    chunks = []
    async for chunk in pipe.pipe(
        {
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "Call the echo_owui tool exactly once with text BRIDGE_OK, "
                        "then reply with exactly BRIDGE_OK."
                    ),
                }
            ]
        },
        __chat_id__="owui-bridge-smoke-test",
        __metadata__={"session_id": "owui-bridge-smoke-test"},
        __tools__=tools,
    ):
        chunks.append(chunk)

    output = "".join(chunks)
    print("CALLS:", calls)
    print("OUTPUT:", output)
    if calls != ["BRIDGE_OK"]:
        raise SystemExit(f"bridge call mismatch: {calls!r}")
    if "BRIDGE_OK" not in output:
        raise SystemExit("model response did not contain BRIDGE_OK")


if __name__ == "__main__":
    asyncio.run(main())
