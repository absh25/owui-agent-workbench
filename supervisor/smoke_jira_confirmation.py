"""Verify OWUI's live editable Jira confirmation transport; ALWAYS cancel.

Run inside the OWUI container after registration and deployment. This creates
and removes one local test chat. Every Socket.IO confirmation callback returns
the literal False; the script never approves a Jira write.
"""

import asyncio
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid

import httpx
import socketio

from governance.confirmation import validate_form
from governance.policy_loader import PolicyRegistry
from open_webui.models.chats import ChatForm, Chats
from open_webui.models.functions import Functions
from open_webui.models.models import Models
from open_webui.models.users import Users
from open_webui.utils.auth import create_token


MODEL_ID = "owui-main"
BASE_URL = "http://127.0.0.1:8080"
WRITE_OPERATIONS = ("create_jira_ticket", "update_jira_ticket", "transition_jira_ticket", "add_jira_comment", "delete_jira_ticket")


def unpack(value):
    for _ in range(6):
        if isinstance(value, str):
            value = json.loads(value)
        elif isinstance(value, list):
            value = next(item["text"] for item in value if isinstance(item, dict) and item.get("type") in {"text", "input_text"})
        elif isinstance(value, dict) and "content" in value and "status" not in value:
            value = value["content"]
        else:
            return value
    raise AssertionError("Tool result did not contain a structured object")


def native_results(message):
    return {item["call_id"]: unpack(item.get("output", [])) for item in message.get("output", [])
            if item.get("type") == "function_call_output"}


async def main():
    started = time.monotonic()
    page = await Users.get_users(limit=1000)
    user = next(u for u in page["users"] if u.name == os.environ.get("OPEN_TERMINAL_OWNER_NAME", "wenguang.wu"))
    token = create_token({"id": user.id})
    chat_id, user_message_id, message_id = (str(uuid.uuid4()) for _ in range(3))
    test_suffix = uuid.uuid4().hex[:8]
    prompt = (
        "请为 KAN 项目准备一条 Jira 测试工单，类型用该项目实际支持的‘任务’。"
        f"标题为‘[测试] 可编辑确认预览 {test_suffix}’，描述为‘本工单仅用于确认弹窗预览验证，无需处理。’。"
        "先只读检查真实项目、权限与类型，然后调用创建工具触发可编辑确认弹窗，等待我在弹窗中决定。"
        "收到取消后立即停止，不重试，不修改参数后再次创建，不执行其他 Jira 写入。"
    )
    socket = socketio.AsyncClient(reconnection=False, logger=False, engineio_logger=False)
    confirmations, callback_errors, observed_statuses = [], [], []
    saved, finished = False, False

    @socket.on("events")
    async def on_event(envelope):
        event = (envelope or {}).get("data", {})
        owns_message = envelope.get("chat_id") == chat_id and envelope.get("message_id") == message_id
        if event.get("type") == "confirmation":
            # This branch returns False even when the payload is malformed or is
            # unrelated to this test. No test path returns consent or an edit grant.
            try:
                if owns_message:
                    form = event.get("data", {}).get("jira_form")
                    assert isinstance(form, dict), "Editable Jira form is missing"
                    assert form.get("version") == 1, "Unexpected editable-form version"
                    assert form.get("request_id"), "Confirmation request ID is missing"
                    assert form.get("operation") == "创建工单", "Expected create preview"
                    fields = form.get("fields")
                    assert isinstance(fields, list), "Fields must be a list"
                    named = {field["name"]: field for field in fields}
                    assert set(named) == {"project_key", "issue_type", "summary", "description", "priority", "assignee_account_id"}
                    assert named["project_key"]["value"] == "KAN", "Incorrect actual Jira project"
                    assert named["summary"]["value"] and named["description"]["value"], "Draft text is missing"
                    options = named["issue_type"].get("options", [])
                    assert any(option["value"] == "任务" for option in options), "Actual task type was not offered"
                    assert named["issue_type"]["value"] in {option["value"] for option in options}, "Selected type is not offered"
                    local_values = {field["name"]: field["value"] for field in fields}
                    local_values["summary"] = "[测试] 本地编辑验证 " + test_suffix
                    local_values["description"] += "\n这段文字仅在测试客户端本地修改，随后取消。"
                    edited = validate_form("create_jira_ticket", fields, local_values)
                    assert edited["summary"] != named["summary"]["value"]
                    assert edited["description"] != named["description"]["value"]
                    confirmations.append({"request_id": form["request_id"], "version": form["version"],
                                          "field_names": list(named), "type_options": options,
                                          "local_edit_validated": True})
            except Exception as exc:
                callback_errors.append(f"{type(exc).__name__}: {exc}")
            finally:
                return False
        if owns_message and event.get("type") == "status":
            observed_statuses.append(event.get("data", {}))
        return None

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=60,
        headers={"Authorization": "Bearer " + token}) as http:
        try:
            # Refuse to trigger even a preview unless the deployed main Pipe and
            # policy contain the required human-confirmation boundary.
            function = await Functions.get_function_by_id(MODEL_ID)
            assert function is not None and function.is_active, "Main Pipe is not active"
            assert "require_user_confirmation=True" in function.content and "UserConfirmationGate" in function.content
            policy = PolicyRegistry(os.environ.get("TOOL_GOVERNANCE_POLICY", "/lab/policies/tool-governance.yaml"))
            assert policy.policy_for("company-jira_create_jira_ticket").get("mode") == "governed"
            response = await http.get("/api/models")
            response.raise_for_status()
            model = next(item for item in response.json()["data"] if item["id"] == MODEL_ID)
            assert model["name"] == "协作助手" and not model.get("info", {}).get("base_model_id")
            tool_ids = model.get("info", {}).get("meta", {}).get("toolIds") or []
            assert {"retrieval_agent", "coding_agent", "server:mcp:company-jira"}.issubset(tool_ids)
            registered_model = await Models.get_model_by_id(MODEL_ID)
            assert registered_model is not None and registered_model.params.function_calling == "native"
            user_message = {"id": user_message_id, "role": "user", "content": prompt, "parentId": None,
                            "childrenIds": [message_id], "models": [MODEL_ID]}
            assistant = {"id": message_id, "role": "assistant", "content": "", "parentId": user_message_id,
                         "childrenIds": [], "model": MODEL_ID, "done": False}
            assert await Chats.insert_new_chat(chat_id, user.id, ChatForm(chat={
                "title": "Jira 可编辑弹窗取消验证 " + test_suffix, "models": [MODEL_ID],
                "history": {"messages": {user_message_id: user_message, message_id: assistant}, "currentId": message_id},
                "messages": [user_message, assistant]})) is not None
            saved = True
            await socket.connect(BASE_URL, socketio_path="ws/socket.io", auth={"token": token},
                                 transports=["websocket"], wait_timeout=20)
            joined = await socket.call("user-join", {"auth": {"token": token}}, timeout=15)
            assert isinstance(joined, dict) and joined.get("id") == user.id, "Socket session did not authenticate as test user"
            session_id = socket.get_sid()
            assert session_id, "Actual authenticated Socket.IO session ID is missing"
            response = await http.post("/api/chat/completions", json={
                "model": MODEL_ID, "messages": [{"role": "user", "content": prompt}],
                "chat_id": chat_id, "id": message_id, "session_id": session_id,
                "stream": True, "tool_ids": tool_ids, "background_tasks": {}})
            response.raise_for_status()
            message = None
            for _ in range(180):
                if not socket.connected:
                    raise RuntimeError("Confirmation socket disconnected; stopping the test chat")
                message = await Chats.get_message_by_id_and_message_id(chat_id, message_id)
                if message and (message.get("done") or message.get("error")):
                    break
                await asyncio.sleep(1)
            assert message and message.get("done") and not message.get("error"), "Native confirmation test did not finish cleanly"
            assert not callback_errors, callback_errors
            assert len(confirmations) == 1, {"error": "Expected exactly one editable confirmation and no repeated write attempt",
                "confirmation_count": len(confirmations), "tool_calls": [item.get("name") for item in message.get("output", [])
                    if item.get("type") == "function_call"], "answer_preview": message.get("content", "")[:600]}
            calls = [item for item in message.get("output", []) if item.get("type") == "function_call"]
            write_calls = [item for item in calls if item.get("name", "").endswith(WRITE_OPERATIONS)]
            assert len(write_calls) == 1 and write_calls[0]["name"].endswith("create_jira_ticket"), "Unexpected repeated or additional Jira write"
            results = native_results(message)
            cancelled = results[write_calls[0].get("call_id") or write_calls[0]["id"]]
            assert cancelled.get("status") == "CANCELLED" and cancelled.get("execution_performed") is False
            assert cancelled.get("request_id") == confirmations[0]["request_id"]
            database = Path(os.environ.get("TOOL_GOVERNANCE_DATABASE", "/app/backend/data/tool-governance.sqlite3"))
            with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
                db.row_factory = sqlite3.Row
                record = db.execute("SELECT status, user_id, chat_id, message_id, execution_result FROM approval_requests WHERE request_id=?",
                                    (confirmations[0]["request_id"],)).fetchone()
                assert record is not None and record["status"] == "CANCELLED"
                assert record["user_id"] == user.id and record["chat_id"] == chat_id and record["message_id"] == message_id
                assert record["execution_result"] is None
                assert db.execute("SELECT COUNT(*) FROM approval_requests WHERE chat_id=? AND status IN ('EXECUTING','EXECUTED','UNKNOWN')",
                                  (chat_id,)).fetchone()[0] == 0
            for statuses in (observed_statuses, message.get("statusHistory", [])):
                confirmation_statuses = [item for item in statuses if any(marker in item.get("description", "")
                    for marker in ("等待你确认", "已取消提交", "未取得有效确认"))]
                assert confirmation_statuses and confirmation_statuses[-1].get("done") is True
                assert "已取消提交" in confirmation_statuses[-1].get("description", "")
            finished = True
            print("LIVE_JIRA_EDITABLE_CONFIRMATION_CANCEL_OK", json.dumps({
                "model": MODEL_ID, "version": confirmations[0]["version"],
                "fields": confirmations[0]["field_names"], "type_options": confirmations[0]["type_options"],
                "local_edit_validated": True, "tool_calls": [item.get("name") for item in calls],
                "approval_status": cancelled["status"], "execution_performed": False,
                "waiting_status_completed": True, "seconds": round(time.monotonic() - started, 2)}, ensure_ascii=False), flush=True)
        finally:
            if saved and not finished:
                try:
                    await http.post("/api/tasks/chat/" + chat_id + "/stop", timeout=15)
                except Exception:
                    pass
            if socket.connected:
                await socket.disconnect()
            if saved:
                await Chats.delete_chat_by_id(chat_id)


if __name__ == "__main__":
    asyncio.run(main())
