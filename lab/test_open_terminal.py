"""Verify the dedicated wenguang.wu Agent workspace end to end."""

import asyncio
import json
import os
import urllib.parse
import urllib.request

from open_webui.models.config import Config
from open_webui.models.users import Users
from open_webui.utils.access_control import has_access
from open_webui.utils.tools import get_tool_servers_data


SERVER_ID = "local-open-terminal"


async def main() -> None:
    connections = await Config.get("terminal_server.connections", []) or []
    connection = next((item for item in connections if item.get("id") == SERVER_ID), None)
    if connection is None:
        raise RuntimeError("OpenTerminal connection is not registered")

    page = await Users.get_users(limit=1000)
    users = page.get("users", []) if isinstance(page, dict) else []
    owner = next((user for user in users if user.name == "wenguang.wu"), None)
    if owner is None:
        raise RuntimeError("Open WebUI user 'wenguang.wu' does not exist")
    grants = (connection.get("config") or {}).get("access_grants", [])
    read_access = await has_access(owner.id, "read", grants, [])
    write_access = await has_access(owner.id, "write", grants, [])
    if not read_access or not write_access:
        raise RuntimeError(f"Incomplete Open WebUI access grants: {grants!r}")

    servers = await get_tool_servers_data(
        [
            {
                "url": connection["url"],
                "key": connection["key"],
                "auth_type": connection.get("auth_type", "bearer"),
                "path": connection.get("path", "/openapi.json"),
                "spec_type": "url",
                "config": {"enable": connection.get("enabled", True)},
                "info": {"id": connection["id"], "name": connection["name"]},
            }
        ]
    )
    if not servers or not servers[0].get("specs"):
        raise RuntimeError("Open WebUI did not load any OpenTerminal tools")

    api_key = os.environ["OPEN_TERMINAL_API_KEY"]
    headers = {"Authorization": f"Bearer {api_key}"}
    cwd = json.load(
        urllib.request.urlopen(
            urllib.request.Request(
                "http://open-terminal:8000/files/cwd", headers=headers
            ),
            timeout=15,
        )
    )
    workspace = "/workspace/wenguang.wu"
    if cwd.get("cwd") != workspace or cwd.get("home") != workspace:
        raise RuntimeError(f"Unexpected workspace metadata: {cwd!r}")
    if (cwd.get("root") or {}).get("label") != "wenguang.wu":
        raise RuntimeError(f"Unexpected file-browser label: {cwd!r}")

    payload = json.dumps(
        {
            "command": (
                "test -r . && test -w . && test -x . && "
                "touch .agent-write-test && rm .agent-write-test && "
                "for c in git curl jq rg fd node npm python soffice pandoc "
                "pdftotext qpdf gs dot ssh rsync sqlite3 shellcheck; do "
                "command -v $c >/dev/null || exit 20; done && "
                "python -c 'import docx,pptx,openpyxl,pypdf,pandas,numpy,PIL,"
                "requests,httpx,yaml,pydantic,sqlalchemy,duckdb,xlrd,odf' && "
                "printf OPEN_TERMINAL_OK"
            ),
            "cwd": workspace,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        "http://open-terminal:8000/execute?wait=10",
        data=payload,
        method="POST",
        headers={**headers, "Content-Type": "application/json"},
    )
    result = json.load(urllib.request.urlopen(request, timeout=15))
    result_text = json.dumps(result, ensure_ascii=False)
    if "OPEN_TERMINAL_OK" not in result_text:
        raise RuntimeError(f"Unexpected execute result: {result_text}")

    listing_url = (
        "http://open-terminal:8000/files/list?directory="
        + urllib.parse.quote(workspace)
    )
    listing = json.load(
        urllib.request.urlopen(
            urllib.request.Request(listing_url, headers=headers), timeout=15
        )
    )

    print("REGISTERED", connection["name"])
    print("ACCESS_GRANTS", grants)
    print("READ_ACCESS", read_access)
    print("WRITE_ACCESS", write_access)
    print("WORKSPACE", cwd)
    print("INITIAL_ENTRIES", [entry["name"] for entry in listing.get("entries", [])])
    print("TOOL_COUNT", len(servers[0]["specs"]))
    print("TOOL_NAMES", [spec.get("name") for spec in servers[0]["specs"]])
    print("READ_WRITE_EXECUTE_OK", True)


if __name__ == "__main__":
    asyncio.run(main())
