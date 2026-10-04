"""Verify the Open WebUI grants for the dedicated OpenTerminal connection."""

import asyncio

from open_webui.models.config import Config
from open_webui.models.users import Users
from open_webui.utils.access_control import has_access


async def main() -> None:
    page = await Users.get_users(limit=1000)
    users = page.get("users", []) if isinstance(page, dict) else []
    owner = next((user for user in users if user.name == "wenguang.wu"), None)
    if owner is None:
        raise RuntimeError("Open WebUI user 'wenguang.wu' does not exist")

    connections = await Config.get("terminal_server.connections", []) or []
    connection = next(
        (item for item in connections if item.get("id") == "local-open-terminal"),
        None,
    )
    if connection is None:
        raise RuntimeError("OpenTerminal connection is not registered")

    grants = (connection.get("config") or {}).get("access_grants", [])
    read_ok = await has_access(owner.id, "read", grants, [])
    write_ok = await has_access(owner.id, "write", grants, [])
    print("CONNECTION", connection["name"])
    print("OWNER", owner.name, owner.id)
    print("GRANTS", grants)
    print("READ_ACCESS", read_ok)
    print("WRITE_ACCESS", write_ok)
    if not read_ok or not write_ok:
        raise RuntimeError("Terminal read/write access is incomplete")


if __name__ == "__main__":
    asyncio.run(main())
