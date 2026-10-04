"""Streamable HTTP MCP facade for Jira Cloud REST v3."""

from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP

from jira_client import JiraClient


mcp = FastMCP(
    "Company Jira MCP",
    instructions=(
        "Read and manage Jira work items. Write operations may be intercepted "
        "by the Open WebUI Tool Execution Middleware for policy or approval."
    ),
    host="0.0.0.0",
    port=8000,
    streamable_http_path="/mcp",
    stateless_http=True,
    json_response=True,
)


def _client() -> JiraClient:
    return JiraClient(
        os.environ.get("JIRA_BASE_URL", ""),
        os.environ.get("JIRA_EMAIL", ""),
        os.environ.get("JIRA_API_TOKEN", ""),
    )


@mcp.tool()
async def search_jira(jql: str, max_results: int = 20) -> dict:
    """使用 JQL 搜索 Jira 工单。这是只读操作。"""
    return await _client().search(jql, max_results)


@mcp.tool()
async def get_jira_ticket(issue_key: str) -> dict:
    """读取一个 Jira 工单的主要字段。这是只读操作。"""
    return await _client().get_issue(issue_key)


@mcp.tool()
async def get_jira_comments(issue_key: str, max_results: int = 50) -> dict:
    """读取 Jira 工单评论。这是只读操作。"""
    return await _client().get_comments(issue_key, max_results)


@mcp.tool()
async def get_jira_create_context(project_key: str = "") -> dict:
    """创建前只读查询：不传项目 Key 时列出真实可见项目；传真实 Key 后检查创建权限并列出可用工单类型。
    用户未指定项目时先查询，不能猜 TEST/DEVS 等 Key。不会创建工单。
    """
    return await _client().get_create_context(project_key)


@mcp.tool()
async def create_jira_ticket(
    project_key: str,
    summary: str,
    description: str,
    issue_type: str = "Task",
    priority: str | None = None,
    assignee_account_id: str | None = None,
) -> dict:
    """创建 Jira 工单。先用 get_jira_create_context 确认真实项目、创建权限和类型，再走 OWUI 人工确认。"""
    return await _client().create_issue(
        project_key=project_key,
        summary=summary,
        description=description,
        issue_type=issue_type,
        priority=priority,
        assignee_account_id=assignee_account_id,
    )


@mcp.tool()
async def update_jira_ticket(
    issue_key: str,
    summary: str | None = None,
    description: str | None = None,
    priority: str | None = None,
    assignee_account_id: str | None = None,
) -> dict:
    """修改 Jira 工单字段。该写操作必须经过 OWUI 治理中间件。"""
    return await _client().update_issue(
        issue_key=issue_key,
        summary=summary,
        description=description,
        priority=priority,
        assignee_account_id=assignee_account_id,
    )


@mcp.tool()
async def transition_jira_ticket(issue_key: str, target_status: str) -> dict:
    """转换 Jira 工单状态；完成和关闭状态默认需要审批。"""
    return await _client().transition_issue(issue_key, target_status)


@mcp.tool()
async def add_jira_comment(issue_key: str, comment: str) -> dict:
    """向 Jira 工单添加评论。该写操作必须经过 OWUI 治理中间件。"""
    return await _client().add_comment(issue_key, comment)


@mcp.tool()
async def delete_jira_ticket(issue_key: str) -> dict:
    """删除 Jira 工单；默认治理策略会拒绝执行。"""
    return await _client().delete_issue(issue_key)


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
