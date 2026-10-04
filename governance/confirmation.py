from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Callable


_OPERATIONS = {
    "create_jira_ticket": "创建工单",
    "update_jira_ticket": "修改工单",
    "transition_jira_ticket": "变更工单状态",
    "add_jira_comment": "添加评论",
}
_FIELDS = {
    "project_key": "项目", "issue_key": "工单", "summary": "标题",
    "description": "描述", "issue_type": "类型", "priority": "优先级",
    "assignee_account_id": "经办人账号", "target_status": "目标状态",
    "comment": "评论内容",
}

_FORM_FIELDS = {
    "create_jira_ticket": ("project_key", "issue_type", "summary", "description", "priority", "assignee_account_id"),
    "update_jira_ticket": ("issue_key", "summary", "description", "priority", "assignee_account_id"),
    "transition_jira_ticket": ("issue_key", "target_status"),
    "add_jira_comment": ("issue_key", "comment"),
}
_REQUIRED = {
    "create_jira_ticket": {"project_key", "issue_type", "summary", "description"},
    "update_jira_ticket": {"issue_key"},
    "transition_jira_ticket": {"issue_key", "target_status"},
    "add_jira_comment": {"issue_key", "comment"},
}


@dataclass(frozen=True)
class ConfirmedArguments:
    """Internal grant produced only after validating the chat client's reply."""
    request_id: str
    tool_name: str
    arguments: dict[str, Any]


def operation_name(tool_name: str) -> str | None:
    return next((name for name in _FORM_FIELDS
                 if tool_name == name or tool_name.endswith("_" + name)), None)


def validate_form(operation: str, fields: list[dict], values: Any) -> dict[str, Any]:
    if not isinstance(values, dict) or set(values) != {field["name"] for field in fields}:
        raise ValueError("提交字段不完整或包含未知字段。")
    result = {}
    for field in fields:
        key, value = field["name"], values[field["name"]]
        if not isinstance(value, str):
            raise ValueError("提交字段必须是文本。")
        if len(value) > field["max_length"] or "\x00" in value:
            raise ValueError(f"{field['label']}内容过长或无效。")
        if field["required"] and not value.strip():
            raise ValueError(f"请填写{field['label']}。")
        if field.get("options") and value not in {option["value"] for option in field["options"]}:
            raise ValueError(f"请选择有效的{field['label']}。")
        result[key] = value if value or field["required"] or key in {"description", "comment"} else None
    if operation == "update_jira_ticket" and all(
        result.get(key) is None for key in ("summary", "description", "priority", "assignee_account_id")
    ):
        raise ValueError("请至少填写一个要修改的字段。")
    return result


class UserConfirmationGate:
    """Ask the authenticated chat client; model arguments cannot approve writes."""

    def __init__(self, event_call: Callable | None, emitter: Callable | None = None):
        self.event_call = event_call
        self.emitter = emitter
        self._waiting = 0
        self._lock = asyncio.Lock()
        self._create_contexts: dict[str, dict] = {}
        self._projects: list[dict] = []

    def observe_create_context(self, result: Any) -> None:
        """Reuse metadata already fetched by this request's authorized read tool."""
        if isinstance(result, tuple):
            result = result[0]
        for _ in range(4):
            if isinstance(result, str):
                try:
                    result = json.loads(result)
                except ValueError:
                    return
            elif isinstance(result, list):
                result = next((item.get("text") for item in result
                               if isinstance(item, dict) and item.get("type") == "text"), None)
            elif isinstance(result, dict) and "content" in result and "status" not in result:
                result = result["content"]
            else:
                break
        if isinstance(result, dict) and result.get("status") == "projects_available":
            self._projects = [item for item in result.get("projects", [])
                              if isinstance(item, dict) and item.get("key")]
        if isinstance(result, dict) and result.get("status") == "ready":
            key = result.get("project", {}).get("key")
            if key:
                self._create_contexts[str(key)] = result

    def _form(self, request_id: str, tool_name: str, arguments: dict) -> dict | None:
        operation = operation_name(tool_name)
        if not operation:
            return None
        fields = []
        for name in _FORM_FIELDS[operation]:
            # Updates show only proposed fields; blank optional fields cannot
            # silently clear unrelated existing issue contents.
            if operation == "update_jira_ticket" and name not in arguments:
                continue
            value = arguments.get(name, "Task" if name == "issue_type" else "")
            field = {"name": name, "label": _FIELDS[name], "value": "" if value is None else str(value),
                     "required": name in _REQUIRED[operation],
                     "multiline": name in {"description", "comment"},
                     "max_length": 255 if name == "summary" else (32000 if name in {"description", "comment"} else 512)}
            if name == "project_key" and any(item["key"] == value for item in self._projects):
                field["options"] = [{"value": item["key"],
                    "label": f"{item['key']} · {item.get('name') or item['key']}"} for item in self._projects]
            if name == "issue_type":
                context = self._create_contexts.get(str(arguments.get("project_key", "")), {})
                types = context.get("issue_types", [])
                options = [{"value": item["name"], "label": item["name"]} for item in types if item.get("name")]
                chosen = next((item["name"] for item in types if str(value).casefold() in
                    {str(item.get(key) or "").casefold() for key in ("id", "name", "untranslated_name")}), None)
                if options and chosen:
                    field.update(options=options, value=chosen)
            fields.append(field)
        return {"version": 1, "request_id": request_id, "operation": _OPERATIONS[operation], "fields": fields}

    def is_waiting(self) -> bool:
        return self._waiting > 0

    async def __call__(self, *, request_id: str, tool_name: str, arguments: dict[str, Any]) -> Any:
        if self.event_call is None:
            return {"error": "确认需要保持当前聊天页面打开，请在浏览器聊天中重新发起操作。"}
        operation = next(
            (label for name, label in _OPERATIONS.items()
             if tool_name == name or tool_name.endswith("_" + name)), tool_name,
        )
        lines = [f"操作：{operation}", ""]
        for key, value in arguments.items():
            rendered = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
            lines.append(f"{_FIELDS.get(key, key)}：{rendered}")
        lines.extend(["", "点击确认后，将以上内容提交到 Jira。"])
        form = self._form(request_id, tool_name, arguments)
        self._waiting += 1
        try:
            async with self._lock:
                description = "未取得有效确认，未提交。"
                try:
                    if self.emitter is not None:
                        try:
                            await self.emitter({"type": "status", "data": {
                                "description": "等待你确认提交内容……", "done": False,
                            }})
                        except Exception:
                            pass
                    data = {"title": "提交 Jira 前确认", "message": "\n".join(lines)}
                    if form:
                        data["jira_form"] = form
                    response = await self.event_call({"type": "confirmation", "data": data})
                    if (isinstance(response, dict) and response.get("confirmed") is True and form
                            and {"request_id", "arguments"}.issubset(response)):
                        if response.get("request_id") != request_id:
                            return {"error": "确认请求已失效，未提交。"}
                        try:
                            values = validate_form(operation_name(tool_name), form["fields"], response.get("arguments"))
                        except ValueError as exc:
                            return {"error": str(exc)}
                        response = ConfirmedArguments(request_id, tool_name, values)
                    if response is True or isinstance(response, ConfirmedArguments):
                        description = "已确认提交内容。"
                    elif response is False:
                        description = "已取消提交。"
                    return response
                finally:
                    # Complete before releasing the lock so this completion cannot
                    # overwrite another confirmation's waiting status.
                    if self.emitter is not None:
                        try:
                            await self.emitter({"type": "status", "data": {
                                "description": description, "done": True,
                            }})
                        except Exception:
                            pass
        finally:
            self._waiting -= 1
