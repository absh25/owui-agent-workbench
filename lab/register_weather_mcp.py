"""Register the local weather MCP in Open WebUI while preserving connections."""

import asyncio
import json
from datetime import datetime
from pathlib import Path

from open_webui.models.config import Config


SERVER_ID = "local-weather"
CONNECTION = {
    "url": "http://weather-mcp:8000/mcp",
    "path": "",
    "type": "mcp",
    "auth_type": "none",
    "forward_cookies": False,
    "headers": {},
    "key": "",
    "config": {"enable": True},
    "info": {
        "id": SERVER_ID,
        "name": "本地天气 MCP",
        "description": "通过 Open-Meteo 查询实时天气和未来 1-7 天预报，无需 API 密钥。",
    },
}


async def main() -> None:
    connections = await Config.get("tool_server.connections", []) or []
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = Path(
        f"/app/backend/data/tool-server-connections.before-weather-{timestamp}.json"
    )
    backup.write_text(json.dumps(connections, ensure_ascii=False, indent=2), encoding="utf-8")
    backup.chmod(0o600)

    updated = []
    replaced = False
    for connection in connections:
        if (connection.get("info") or {}).get("id") == SERVER_ID:
            updated.append(CONNECTION)
            replaced = True
        else:
            updated.append(connection)
    if not replaced:
        updated.append(CONNECTION)

    await Config.upsert({"tool_server.connections": updated})
    print("REGISTERED", SERVER_ID)
    print("CONNECTION_COUNT", len(updated))
    print("BACKUP", backup)


if __name__ == "__main__":
    asyncio.run(main())
