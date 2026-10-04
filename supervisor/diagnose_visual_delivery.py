"""Read-only inspection of the pelican animation chat's delivery evidence."""

import json
import os
import sqlite3

import httpx


def main():
    with sqlite3.connect("file:/app/backend/data/webui.db?mode=ro", uri=True) as db:
        rows = db.execute(
            "SELECT id, title, chat FROM chat WHERE chat LIKE ? ORDER BY updated_at DESC LIMIT 2",
            ("%pelican_riding_bike%",),
        ).fetchall()
        for chat_id, title, raw in rows:
            chat = json.loads(raw)
            messages = list((chat.get("history") or {}).get("messages", {}).values())
            if not messages:
                messages = chat.get("messages") or []
            print(json.dumps({
                "chat_id": chat_id, "title": title, "models": chat.get("models"),
                "messages": [{key: message.get(key) for key in (
                    "id", "role", "model", "content", "tool_calls", "files", "statusHistory")
                } for message in messages[-4:]],
            }, ensure_ascii=False))

    headers = {"Authorization": "Bearer " + os.environ["OPEN_TERMINAL_API_KEY"]}
    with httpx.Client(base_url="http://open-terminal:8000", headers=headers, timeout=20) as client:
        for path in ("/home/user/pelican_riding_bike.html", "/workspace/wenguang.wu/pelican_riding_bike.html"):
            response = client.get("/files/read", params={"path": path})
            print(json.dumps({"path": path, "status": response.status_code,
                              "bytes": len(response.content),
                              "content_type": response.headers.get("content-type")}, ensure_ascii=False))
            if response.is_success:
                data = response.json()
                print(json.dumps({"file_response_fields": list(data), "sample": {
                    key: str(value)[:120] for key, value in data.items()
                }}, ensure_ascii=False))


if __name__ == "__main__":
    main()
