"""Read-only integration test through OWUI's real saved-chat native loop."""

import asyncio
import json
import os
import sys
import uuid

import httpx
from open_webui.models.chats import Chats, ChatForm
from open_webui.models.users import Users
from open_webui.utils.auth import create_token


async def main():
    page = await Users.get_users(limit=1000)
    user = next(u for u in page["users"] if u.name == os.environ.get("OPEN_TERMINAL_OWNER_NAME", "wenguang.wu"))
    chat_id, user_id, message_id = (str(uuid.uuid4()) for _ in range(3))
    coding = "--coding" in sys.argv
    if coding:
        repo = sys.argv[sys.argv.index("--coding") + 1]
        prompt = f"请分析代码项目 {repo}：读取 calculator.py 和 test_calculator.py，运行 python -m unittest -v，给出结果。不要提交、push 或修改 Jira。"
    else:
        prompt = "使用 Jira 工具查询 KAN-3，返回编号、标题和当前状态。只读查询，不修改任何内容。"
    user_message = {"id": user_id, "role": "user", "content": prompt, "parentId": None,
                    "childrenIds": [message_id], "models": ["owui-main"]}
    assistant = {"id": message_id, "role": "assistant", "content": "", "parentId": user_id,
                 "childrenIds": [], "model": "owui-main"}
    chat = await Chats.insert_new_chat(chat_id, user.id, ChatForm(chat={
        "title": "主助手原生循环验证", "models": ["owui-main"],
        "history": {"messages": {user_id: user_message, message_id: assistant}, "currentId": message_id},
        "messages": [user_message, assistant]}))
    if chat is None:
        raise RuntimeError("Cannot create integration test chat")
    try:
        async with httpx.AsyncClient(base_url="http://127.0.0.1:8080", timeout=120,
            headers={"Authorization": "Bearer " + create_token({"id": user.id})}) as client:
            models = await client.get("/api/models")
            models.raise_for_status()
            assert "owui-main" in {m["id"] for m in models.json()["data"]}
            response = await client.post("/api/chat/completions", json={
                "model": "owui-main", "messages": [{"role": "user", "content": prompt}],
                "chat_id": chat_id, "id": message_id, "stream": True,
                "tool_ids": ["coding_agent", "server:mcp:company-jira"],
                "background_tasks": {}})
            response.raise_for_status()
            print("HTTP_RESPONSE", response.text[:350], flush=True)
        for _ in range(90):
            message = await Chats.get_message_by_id_and_message_id(chat_id, message_id)
            if message and (message.get("done") or message.get("error")):
                print("NATIVE_RESULT", json.dumps(message, ensure_ascii=False), flush=True)
                names = [o.get("name", "") for o in message.get("output", []) if o.get("type") == "function_call"]
                expected = "delegate_coding_task" if coding else "get_jira_ticket"
                if message.get("error") or not any(n.endswith(expected) for n in names):
                    raise RuntimeError("Expected native tool call did not complete")
                print("NATIVE_CODING_LOOP_OK" if coding else "NATIVE_BUSINESS_LOOP_OK", flush=True)
                return
            await asyncio.sleep(1)
        raise RuntimeError("Native business loop did not finish")
    finally:
        await Chats.delete_chat_by_id(chat_id)


if __name__ == "__main__":
    asyncio.run(main())
