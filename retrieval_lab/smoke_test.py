"""Real Bailian -> read-only MCP -> persisted OWUI native-loop integration."""

import asyncio
import json
import os
import uuid

import httpx
from open_webui.models.chats import Chats, ChatForm
from open_webui.models.users import Users
from open_webui.utils.auth import create_token
from open_webui.utils.mcp.client import MCPClient


FILENAME = "演示-频谱感知实验.md"
DOCUMENT = """# 频谱感知链路演示

这是一份虚构的集成测试资料，仅用于验证上传、向量化、检索和引用。
这些参数不是工程标准，不应据此进行真实实验。

## 实验 SS-TEST-042
演示实验 SS-TEST-042 的采样率设为 2 MHz，能量检测阈值设为 0.12。
继续执行前须核对真实实验的噪声标定记录与适用条件。
文档没有给出通用阈值计算方法，也没有提供真实检测性能结果。
"""


def unpack(content):
    return json.loads(next(block["text"] for block in content if block["type"] == "text"))


async def native_loop(document_id):
    page = await Users.get_users(limit=1000)
    user = next(u for u in page["users"] if u.name == os.environ.get("OPEN_TERMINAL_OWNER_NAME", "wenguang.wu"))
    chat_id, user_id, message_id = (str(uuid.uuid4()) for _ in range(3))
    prompt = "请检索 ragflow 知识库：演示实验 SS-TEST-042 的采样率和能量检测阈值是多少？核对原文并引用文档名和分块，说明这些数据是否能作为工程标准。只查询，不进行任何提交。"
    user_message = {"id": user_id, "role": "user", "content": prompt, "parentId": None,
                    "childrenIds": [message_id], "models": ["owui-main"]}
    assistant = {"id": message_id, "role": "assistant", "content": "", "parentId": user_id,
                 "childrenIds": [], "model": "owui-main"}
    chat = await Chats.insert_new_chat(chat_id, user.id, ChatForm(chat={
        "title": "知识库检索集成测试", "models": ["owui-main"],
        "history": {"messages": {user_id: user_message, message_id: assistant}, "currentId": message_id},
        "messages": [user_message, assistant]}))
    if chat is None:
        raise RuntimeError("Cannot create test chat")
    try:
        async with httpx.AsyncClient(base_url="http://127.0.0.1:8080", timeout=120,
            headers={"Authorization": "Bearer " + create_token({"id": user.id})}) as client:
            models = await client.get("/api/models")
            models.raise_for_status()
            response = await client.post("/api/chat/completions", json={
                "model": "owui-main", "messages": [{"role": "user", "content": prompt}],
                "chat_id": chat_id, "id": message_id, "stream": True,
                "tool_ids": ["retrieval_agent"], "background_tasks": {}})
            response.raise_for_status()
        for _ in range(120):
            message = await Chats.get_message_by_id_and_message_id(chat_id, message_id)
            if message and (message.get("done") or message.get("error")):
                names = [o.get("name", "") for o in message.get("output", []) if o.get("type") == "function_call"]
                assert not message.get("error"), message.get("error")
                assert any(n.endswith("delegate_retrieval_task") for n in names), names
                assert not any(n.endswith("search_knowledge") for n in names), names
                answer = message.get("content", "")
                assert "0.12" in answer and FILENAME in answer and "分块" in answer, answer
                assert "2" in answer and "MHz" in answer, answer
                assert any(word in answer for word in ("虚构", "演示", "测试资料")), answer
                print("OWUI_NATIVE_RETRIEVAL_OK", json.dumps({"tool_calls": names, "answer": answer}, ensure_ascii=False), flush=True)
                return
            await asyncio.sleep(1)
        raise RuntimeError("OWUI native retrieval did not finish")
    finally:
        await Chats.delete_chat_by_id(chat_id)


async def main():
    base = "http://retrieval-lab:8000"
    headers = {"Authorization": "Bearer " + os.environ["RAGFLOW_MCP_TOKEN"]}
    async with httpx.AsyncClient(base_url=base, headers=headers, timeout=120) as client:
        health = await client.get("/health")
        health.raise_for_status()
        assert health.json()["embedding"]["key_configured"]
        assert (await client.get("/")).status_code == 200
        assert (await client.get("/mcp", headers={"Authorization": ""})).status_code == 401
        assert (await client.get("/api/overview", headers={"Host": "untrusted.example"})).status_code == 421
        assert (await client.post("/api/settings", json={}, headers={"Authorization": ""})).status_code == 403
        assert (await client.post("/api/settings", json={}, headers={"Origin": "https://untrusted.example"})).status_code == 403
        public = await client.get("/api/overview")
        public.raise_for_status()
        assert "api_key" not in public.json()["embedding"]
        response = await client.post("/api/documents", files={"file": (FILENAME, DOCUMENT.encode(), "text/markdown")})
        response.raise_for_status()
        document_id = response.json()["id"]
        response = await client.post(f"/api/documents/{document_id}/index")
        response.raise_for_status()
        assert response.json()["status"] == "ready"
        query = "SS-TEST-042 的采样率和阈值"
        response = await client.post("/api/search", json={"query": query})
        response.raise_for_status()
        hits = response.json()["results"]
        assert hits and hits[0]["document_id"] == document_id, hits
        assert "2 MHz" in hits[0]["text"] and "0.12" in hits[0]["text"]
        print("BAILIAN_UPLOAD_RETRIEVAL_OK", json.dumps({"filename": FILENAME,
              "matches": len(hits), "score": hits[0]["score"], "citation": hits[0]["citation"]}, ensure_ascii=False), flush=True)
    mcp = MCPClient()
    try:
        await mcp.connect(base + "/mcp", headers=headers)
        specs = await mcp.list_tool_specs()
        names = {spec["name"] for spec in specs}
        assert names == {"search_knowledge", "list_knowledge_documents", "read_knowledge_chunks"}, names
        docs = unpack(await mcp.call_tool("list_knowledge_documents", {}))
        assert docs["indexed_chunk_count"] > 0
        result = unpack(await mcp.call_tool("search_knowledge", {"query": query, "document_id": document_id}))
        assert result["results"][0]["document_id"] == document_id
        original = unpack(await mcp.call_tool("read_knowledge_chunks", {"document_id": document_id}))
        assert "0.12" in original["chunks"][0]["text"]
        print("READ_ONLY_MCP_OK", sorted(names), flush=True)
    finally:
        await mcp.disconnect()
    await native_loop(document_id)


if __name__ == "__main__":
    asyncio.run(main())
