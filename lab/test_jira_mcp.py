"""Verify that Open WebUI can discover the local Jira MCP tools."""

import asyncio

from open_webui.utils.mcp.client import MCPClient


EXPECTED = {
    "search_jira",
    "get_jira_ticket",
    "get_jira_comments",
    "get_jira_create_context",
    "create_jira_ticket",
    "update_jira_ticket",
    "transition_jira_ticket",
    "add_jira_comment",
    "delete_jira_ticket",
}


async def main() -> None:
    client = MCPClient()
    await client.connect("http://jira-mcp:8000/mcp")
    try:
        specs = await client.list_tool_specs()
    finally:
        await client.disconnect()
    names = {item["name"] for item in specs}
    print("TOOLS", sorted(names))
    missing = EXPECTED - names
    if missing:
        raise SystemExit(f"Missing Jira MCP tools: {sorted(missing)}")
    print("JIRA_MCP_DISCOVERY_OK")


if __name__ == "__main__":
    asyncio.run(main())
