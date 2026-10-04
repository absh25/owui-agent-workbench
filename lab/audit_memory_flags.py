"""Inspect memory switches without printing user memories or credentials."""

import asyncio

from open_webui.models.config import Config


async def main():
    for name in ("memories.enable", "memories.system_context.enable"):
        print(name, await Config.get(name))


asyncio.run(main())
