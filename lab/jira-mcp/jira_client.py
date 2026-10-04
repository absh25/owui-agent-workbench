"""Small Jira Cloud REST v3 client used by the local MCP server."""

from __future__ import annotations

from typing import Any, Optional
from urllib.parse import quote

import httpx


class JiraConfigurationError(RuntimeError):
    pass


class JiraAPIError(RuntimeError):
    pass


def adf_text(text: str) -> dict[str, Any]:
    paragraphs = []
    for line in (text or "").splitlines() or [""]:
        content = [{"type": "text", "text": line}] if line else []
        paragraphs.append({"type": "paragraph", "content": content})
    return {"type": "doc", "version": 1, "content": paragraphs}


def _named(field: Any) -> Optional[str]:
    return field.get("name") if isinstance(field, dict) else None


def _user(field: Any) -> Optional[dict[str, Any]]:
    if not isinstance(field, dict):
        return None
    return {
        "account_id": field.get("accountId"),
        "display_name": field.get("displayName"),
        "email": field.get("emailAddress"),
    }


def normalize_issue(issue: dict[str, Any], base_url: str) -> dict[str, Any]:
    fields = issue.get("fields") or {}
    key = issue.get("key")
    return {
        "id": issue.get("id"),
        "key": key,
        "url": f"{base_url.rstrip('/')}/browse/{key}" if key else None,
        "summary": fields.get("summary"),
        "description": fields.get("description"),
        "status": _named(fields.get("status")),
        "priority": _named(fields.get("priority")),
        "issue_type": _named(fields.get("issuetype")),
        "project": _named(fields.get("project")),
        "assignee": _user(fields.get("assignee")),
        "reporter": _user(fields.get("reporter")),
        "created": fields.get("created"),
        "updated": fields.get("updated"),
        "labels": fields.get("labels") or [],
    }


class JiraClient:
    def __init__(self, base_url: str, email: str, api_token: str, timeout: float = 30):
        self.base_url = base_url.rstrip("/")
        self.email = email.strip()
        self.api_token = api_token.strip()
        self.timeout = timeout

    def _validate(self) -> None:
        missing = [
            name
            for name, value in {
                "JIRA_BASE_URL": self.base_url,
                "JIRA_EMAIL": self.email,
                "JIRA_API_TOKEN": self.api_token,
            }.items()
            if not value
        ]
        if missing:
            raise JiraConfigurationError(
                "Jira MCP is not configured; missing " + ", ".join(missing)
            )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        json_body: Optional[dict[str, Any]] = None,
    ) -> Any:
        self._validate()
        async with httpx.AsyncClient(
            base_url=self.base_url,
            auth=httpx.BasicAuth(self.email, self.api_token),
            timeout=self.timeout,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        ) as client:
            response = await client.request(method, path, params=params, json=json_body)
        if response.is_error:
            detail = response.text[:2000]
            raise JiraAPIError(
                f"Jira API {method} {path} failed with HTTP {response.status_code}: {detail}"
            )
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    async def search(self, jql: str, max_results: int = 20) -> dict[str, Any]:
        max_results = max(1, min(int(max_results), 100))
        data = await self._request(
            "POST",
            "/rest/api/3/search/jql",
            json_body={
                "jql": jql,
                "maxResults": max_results,
                "fields": [
                    "summary", "description", "status", "priority", "issuetype",
                    "project", "assignee", "reporter", "created", "updated", "labels",
                ],
            },
        )
        issues = data.get("issues") or []
        return {
            "issues": [normalize_issue(issue, self.base_url) for issue in issues],
            "next_page_token": data.get("nextPageToken"),
            "is_last": data.get("isLast"),
        }

    async def get_issue(self, issue_key: str) -> dict[str, Any]:
        data = await self._request(
            "GET",
            f"/rest/api/3/issue/{issue_key}",
            params={
                "fields": "summary,description,status,priority,issuetype,project,assignee,reporter,created,updated,labels"
            },
        )
        return normalize_issue(data, self.base_url)

    async def get_comments(self, issue_key: str, max_results: int = 50) -> dict[str, Any]:
        data = await self._request(
            "GET",
            f"/rest/api/3/issue/{issue_key}/comment",
            params={"maxResults": max(1, min(int(max_results), 100))},
        )
        return data

    async def get_create_context(self, project_key: str = "") -> dict[str, Any]:
        """Discover real projects and creation permissions/types without writes."""
        key = project_key.strip()
        if not key:
            data = await self._request("GET", "/rest/api/3/project/search",
                                       params={"startAt": 0, "maxResults": 50})
            values = data.get("values") or []
            next_start = int(data.get("startAt", 0)) + len(values)
            is_last = bool(data.get("isLast", next_start >= int(data.get("total", next_start))))
            return {"status": "projects_available",
                    "projects": [{field: p.get(field) for field in ("id", "key", "name")} for p in values],
                    "is_last": is_last, "next_start_at": None if is_last else next_start}
        encoded = quote(key, safe="")
        project = await self._request("GET", f"/rest/api/3/project/{encoded}")
        canonical_key = project.get("key") or key
        permissions = await self._request("GET", "/rest/api/3/mypermissions",
            params={"projectKey": canonical_key, "permissions": "CREATE_ISSUES"})
        allowed = permissions.get("permissions", {}).get("CREATE_ISSUES", {}).get("havePermission") is True
        result = {"status": "ready" if allowed else "create_not_permitted",
                  "project": {field: project.get(field) for field in ("id", "key", "name")},
                  "can_create": allowed, "issue_types": []}
        if allowed:
            types = await self._request("GET",
                f"/rest/api/3/issue/createmeta/{quote(canonical_key, safe='')}/issuetypes",
                params={"startAt": 0, "maxResults": 100})
            result["issue_types"] = [{"id": t.get("id"), "name": t.get("name"),
                "untranslated_name": t.get("untranslatedName"), "subtask": bool(t.get("subtask"))}
                for t in types.get("issueTypes", []) if not t.get("subtask")]
        return result

    async def create_issue(
        self,
        *,
        project_key: str,
        summary: str,
        description: str,
        issue_type: str = "Task",
        priority: Optional[str] = None,
        assignee_account_id: Optional[str] = None,
    ) -> dict[str, Any]:
        if not project_key.strip():
            raise ValueError("需要真实的 Jira 项目 Key；请先调用 get_jira_create_context 查询。")
        context = await self.get_create_context(project_key)
        if not context.get("can_create"):
            raise JiraAPIError("当前账号没有目标项目的工单创建权限。")
        wanted = issue_type.strip().casefold()
        selected = next((item for item in context.get("issue_types", [])
            if wanted in {str(item.get(field) or "").casefold()
                          for field in ("id", "name", "untranslated_name")}), None)
        if selected is None or not selected.get("id"):
            available = [item.get("name") for item in context.get("issue_types", [])]
            raise ValueError(f"目标项目没有可用的工单类型 {issue_type!r}，实际类型：{available}")
        fields: dict[str, Any] = {
            "project": {"key": context["project"]["key"]},
            "summary": summary,
            "description": adf_text(description),
            "issuetype": {"id": selected["id"]},
        }
        if priority:
            fields["priority"] = {"name": priority}
        if assignee_account_id:
            fields["assignee"] = {"accountId": assignee_account_id}
        data = await self._request(
            "POST", "/rest/api/3/issue", json_body={"fields": fields}
        )
        key = data.get("key")
        return {
            "id": data.get("id"),
            "key": key,
            "url": f"{self.base_url}/browse/{key}" if key else None,
        }

    async def update_issue(
        self,
        *,
        issue_key: str,
        summary: Optional[str] = None,
        description: Optional[str] = None,
        priority: Optional[str] = None,
        assignee_account_id: Optional[str] = None,
    ) -> dict[str, Any]:
        fields: dict[str, Any] = {}
        if summary is not None:
            fields["summary"] = summary
        if description is not None:
            fields["description"] = adf_text(description)
        if priority is not None:
            fields["priority"] = {"name": priority}
        if assignee_account_id is not None:
            fields["assignee"] = {"accountId": assignee_account_id}
        if not fields:
            raise ValueError("At least one Jira field must be supplied for update")
        await self._request(
            "PUT", f"/rest/api/3/issue/{issue_key}", json_body={"fields": fields}
        )
        return {"key": issue_key, "url": f"{self.base_url}/browse/{issue_key}"}

    async def transition_issue(self, issue_key: str, target_status: str) -> dict[str, Any]:
        data = await self._request(
            "GET", f"/rest/api/3/issue/{issue_key}/transitions"
        )
        transitions = data.get("transitions") or []
        target = next(
            (
                item
                for item in transitions
                if str(item.get("id")) == target_status
                or str(item.get("name", "")).casefold() == target_status.casefold()
            ),
            None,
        )
        if target is None:
            available = [item.get("name") for item in transitions]
            raise JiraAPIError(
                f"No transition named {target_status!r} is available for {issue_key}; available={available}"
            )
        await self._request(
            "POST",
            f"/rest/api/3/issue/{issue_key}/transitions",
            json_body={"transition": {"id": str(target["id"])}},
        )
        return {
            "key": issue_key,
            "transition": target.get("name"),
            "transition_id": str(target.get("id")),
            "url": f"{self.base_url}/browse/{issue_key}",
        }

    async def add_comment(self, issue_key: str, comment: str) -> dict[str, Any]:
        data = await self._request(
            "POST",
            f"/rest/api/3/issue/{issue_key}/comment",
            json_body={"body": adf_text(comment)},
        )
        return {"issue_key": issue_key, "comment_id": data.get("id")}

    async def delete_issue(self, issue_key: str) -> dict[str, Any]:
        await self._request("DELETE", f"/rest/api/3/issue/{issue_key}")
        return {"key": issue_key, "deleted": True}
