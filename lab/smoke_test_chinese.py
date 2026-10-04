"""Verify that the Pipe answers an English prompt in Simplified Chinese."""

import asyncio
import importlib.util
import re


SPEC = importlib.util.spec_from_file_location("claude_pipe", "/lab/claude_agent_pipe.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


async def main() -> None:
    pipe = MODULE.Pipe()
    chunks = []
    async for chunk in pipe.pipe(
        {
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "What language will you use when communicating with me? "
                        "Answer in one short sentence."
                    ),
                }
            ]
        },
        __chat_id__="chinese-language-smoke-test",
        __metadata__={"session_id": "chinese-language-smoke-test"},
        __tools__={},
    ):
        chunks.append(chunk)

    output = "".join(chunks).strip()
    print("OUTPUT", output)
    if not re.search(r"[\u4e00-\u9fff]", output):
        raise RuntimeError("The model did not answer in Chinese")
    print("CHINESE_OUTPUT_OK", True)


if __name__ == "__main__":
    asyncio.run(main())
