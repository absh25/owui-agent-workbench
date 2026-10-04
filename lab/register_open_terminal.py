"""Register the isolated OpenTerminal service in Open WebUI."""

import asyncio
import json
import os
from datetime import datetime
from pathlib import Path

from open_webui.models.config import Config
from open_webui.models.users import Users


SERVER_ID = "local-open-terminal"


async def main() -> None:
    api_key = os.environ.get("OPEN_TERMINAL_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPEN_TERMINAL_API_KEY is not configured")

    owner_name = os.environ.get("OPEN_TERMINAL_OWNER_NAME", "wenguang.wu").strip()
    page = await Users.get_users(limit=1000)
    users = page.get("users", []) if isinstance(page, dict) else []
    owner = next((user for user in users if user.name == owner_name), None)
    if owner is None:
        raise RuntimeError(f"Open WebUI user {owner_name!r} does not exist")

    connection = {
        "id": SERVER_ID,
        "name": f"{owner_name} 的 OpenTerminal",
        "enabled": True,
        "url": "http://open-terminal:8000",
        "path": "/openapi.json",
        "key": api_key,
        "auth_type": "bearer",
        "forward_cookies": False,
        "config": {
            "access_grants": [
                {
                    "principal_type": "user",
                    "principal_id": owner.id,
                    "permission": "read",
                },
                {
                    "principal_type": "user",
                    "principal_id": owner.id,
                    "permission": "write",
                },
            ]
        },
    }

    connections = await Config.get("terminal_server.connections", []) or []
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = Path(
        f"/app/backend/data/terminal-server-connections.before-local-{timestamp}.json"
    )
    backup.write_text(
        json.dumps(connections, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    backup.chmod(0o600)

    updated = []
    replaced = False
    for item in connections:
        if item.get("id") == SERVER_ID:
            updated.append(connection)
            replaced = True
        else:
            updated.append(item)
    if not replaced:
        updated.append(connection)

    await Config.upsert({"terminal_server.connections": updated})
    print("REGISTERED", SERVER_ID)
    print("OWNER", owner.name, owner.id)
    print("CONNECTION_COUNT", len(updated))
    print("BACKUP", backup)


if __name__ == "__main__":
    asyncio.run(main())
