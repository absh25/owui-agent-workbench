"""Time the text-only fallback used after a completed tool's SDK reply stalls."""

import asyncio
import importlib.util
import time


SPEC = importlib.util.spec_from_file_location("claude_pipe", "/lab/claude_agent_pipe.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


async def main() -> None:
    started = time.monotonic()
    answer = await MODULE.Pipe()._summarize_completed_tools(
        "上海的天气是啥？",
        '{"location":"上海, 中国","condition":"小雨","temperature_c":24.0,'
        '"source":"Open-Meteo"}',
    )
    print("ELAPSED", round(time.monotonic() - started, 2))
    print("ANSWER", answer)
    assert answer and "上海" in answer and "24" in answer


asyncio.run(main())
