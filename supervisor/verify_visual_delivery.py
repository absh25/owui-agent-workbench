"""Verify the repaired chat and authenticated file download without model calls."""

import asyncio
import hashlib
import json
import sqlite3

import httpx

from supervisor.artifacts import previews
from supervisor.recover_visual_delivery import CHAT_ID, MESSAGE_ID, DATABASE, TARGET, MARKER


async def main():
    from open_webui.utils.auth import create_token
    from open_webui.models.functions import Functions
    from open_webui.models.tools import Tools

    with sqlite3.connect("file:" + str(DATABASE) + "?mode=ro", uri=True) as db:
        user_id, raw = db.execute("SELECT user_id, chat FROM chat WHERE id=?", (CHAT_ID,)).fetchone()
    chat = json.loads(raw)
    message = chat["history"]["messages"][MESSAGE_ID]
    assert MARKER in message["content"] and TARGET in message["content"]
    attachment = message["files"][-1]
    token = create_token({"id": user_id})
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8080", timeout=20,
                                  headers={"Authorization": "Bearer " + token}) as client:
        response = await client.get(attachment["url"])
        response.raise_for_status()
        html = response.content.decode("utf-8")
        assert html in message["content"], "Preview does not match downloaded HTML"
        chat_response = await client.get("/api/v1/chats/" + CHAT_ID)
        chat_response.raise_for_status()
        saved = chat_response.json()["chat"]["history"]["messages"][MESSAGE_ID]
        assert html in saved["content"] and saved["files"][-1]["id"] == attachment["id"]
    model_messages = [{"role": "user", "content": "生成动画"},
        {"role": "assistant", "tool_calls": [{"id": "coding", "function": {"name": "delegate_coding_task"}}]},
        {"role": "tool", "tool_call_id": "coding", "content": json.dumps({
            "status": "completed", "artifacts": [{"file_id": attachment["id"]}]})}]
    assert html in await previews(model_messages, user_id), "Runtime preview rendering failed"
    output = message.get("output")
    print(json.dumps({"download_status": response.status_code, "bytes": len(response.content),
        "sha256": hashlib.sha256(response.content).hexdigest(), "chat_api_verified": True,
        "runtime_preview_verified": True, "message_output_type": type(output).__name__,
        "message_keys": list(message),
        "main_model": (await Functions.get_function_valves_by_id("owui-main"))["MODEL"],
        "coding_model": (await Tools.get_tool_valves_by_id("coding_agent"))["MODEL"]}, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
