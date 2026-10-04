"""Read-only live Jira project/permission/type discovery through MCP and OWUI."""

import asyncio
import json
import os

from open_webui.models.users import Users
from open_webui.utils.mcp.client import MCPClient
from supervisor.smoke_agents import saved_chat, tool_results


async def main():
    client = MCPClient()
    try:
        await client.connect("http://jira-mcp:8000/mcp")

        def unpack(rows):
            return json.loads(next(b["text"] for b in rows if b.get("type") == "text"))

        listing = unpack(await client.call_tool("get_jira_create_context", {}))
        assert listing["status"] == "projects_available" and listing["projects"], listing
        project = listing["projects"][0]
        context = unpack(await client.call_tool("get_jira_create_context", {"project_key": project["key"]}))
        assert context["can_create"] is True and context["status"] == "ready", context
        assert context["issue_types"] and all(not t["subtask"] for t in context["issue_types"]), context
        print("JIRA_READ_ONLY_CREATE_CONTEXT_OK", json.dumps({"project": context["project"],
            "can_create": context["can_create"], "issue_types": context["issue_types"]}, ensure_ascii=False), flush=True)
    finally:
        await client.disconnect()

    page = await Users.get_users(limit=1000)
    user = next(u for u in page["users"] if u.name == os.environ.get("OPEN_TERMINAL_OWNER_NAME", "wenguang.wu"))
    message = await saved_chat(user,
        "请只读查询我的 Jira 实际可用项目、目标项目创建权限和可用工单类型，为一条标题为“验证 Jira 集成链路”的"
        "测试工单整理预览。尚未指定项目，请先查询真实项目再选择；不要猜项目 Key。仅展示预览，不调用任何写入工具。")
    calls = [i for i in message.get("output", []) if i.get("type") == "function_call"]
    names = [i.get("name", "") for i in calls]
    context_calls = [i for i in calls if i.get("name", "").endswith("get_jira_create_context")]
    assert context_calls, names
    forbidden = ("create_jira_ticket", "update_jira_ticket", "transition_jira_ticket", "add_jira_comment", "delete_jira_ticket",
                 "delegate_coding_task", "delegate_retrieval_task")
    assert not any(n.endswith(forbidden) for n in names), names
    outputs = tool_results(message)
    actual = [outputs[c.get("call_id") or c.get("id")] for c in context_calls]
    assert any(r.get("status") == "projects_available" for r in actual), actual
    ready = next(r for r in actual if r.get("status") == "ready" and r.get("can_create") is True)
    answer = message.get("content", "")
    assert ready["project"]["key"] in answer, answer
    assert any(t["name"] in answer or t["untranslated_name"] in answer for t in ready["issue_types"]), answer
    print("OWUI_JIRA_PREFLIGHT_PREVIEW_OK", json.dumps({"tool_calls": names, "project": ready["project"],
        "can_create": ready["can_create"], "answer": answer, "write_performed": False}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
