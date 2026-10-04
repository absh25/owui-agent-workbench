"""Recover this animation's existing output; optional narrow runtime source sync."""

import argparse
import asyncio
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import sqlite3

import httpx

from supervisor.artifacts import html_markdown, save_html


CHAT_ID = "e2998a75-7a3a-40a9-b63e-3a7d881caa20"
MESSAGE_ID = "e4415f75-0cf7-48fe-bd59-773687fa9633"
SOURCE = "/home/user/pelican_riding_bike.html"
TARGET = "/workspace/wenguang.wu/pelican_riding_bike.html"
MARKER = "<!-- recovered-animation-delivery -->"
DATABASE = Path("/app/backend/data/webui.db")


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--sync", action="store_true")
    args = parser.parse_args()
    with sqlite3.connect("file:" + str(DATABASE) + "?mode=ro", uri=True) as db:
        user_id, raw = db.execute("SELECT user_id, chat FROM chat WHERE id=?", (CHAT_ID,)).fetchone()
    chat = json.loads(raw)
    message = chat["history"]["messages"][MESSAGE_ID]
    headers = {"Authorization": "Bearer " + os.environ["OPEN_TERMINAL_API_KEY"]}
    async with httpx.AsyncClient(base_url="http://open-terminal:8000", headers=headers, timeout=20) as client:
        response = await client.get("/files/read", params={"path": SOURCE})
        response.raise_for_status()
        payload = response.json()
        html = payload["content"]
        assert len(html.splitlines()) == payload["total_lines"], "Incomplete original HTML"
        assert "<html" in html.lower() and "<svg" in html.lower(), "Unexpected animation output"
        if not args.apply:
            print("ANIMATION_EXPORT=" + json.dumps({"html": html}, ensure_ascii=False))
            return

        backup = DATABASE.with_name("webui.db.bak-before-animation-delivery-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
        with sqlite3.connect(DATABASE) as source, sqlite3.connect(backup) as target:
            source.backup(target)
        backup.chmod(0o600)

        response = await client.get("/files/read", params={"path": TARGET})
        if response.status_code == 404:
            process = await client.post("/execute", json={"command": f"cp --no-clobber -- {SOURCE} {TARGET}"})
            process.raise_for_status()
            state = await client.get(f"/execute/{process.json()['id']}/status", params={"wait": 10})
            state.raise_for_status()
            assert state.json().get("exit_code") == 0, "Copy did not complete"
            response = await client.get("/files/read", params={"path": TARGET})
        response.raise_for_status()
        assert response.json()["content"] == html, "Visible directory contains different user content"

        if MARKER not in message["content"]:
            artifact = await save_html(user_id, Path(TARGET).name, html)
            original = message["content"]
            updated = original.replace(SOURCE, TARGET) + "\n\n" + MARKER + html_markdown(
                artifact["name"], artifact["url"], html)
            attachment = {"type": "file", "id": artifact["file_id"], "name": artifact["name"],
                          "url": artifact["url"], "content_type": "text/html"}
            for node in [*chat["history"]["messages"].values(), *(chat.get("messages") or [])]:
                if node.get("id") == MESSAGE_ID:
                    node["content"] = updated
                    node["files"] = [*(node.get("files") or []), attachment]
            with sqlite3.connect(DATABASE) as db:
                changed = db.execute("UPDATE chat SET chat=? WHERE id=? AND chat=?", (
                    json.dumps(chat, ensure_ascii=False), CHAT_ID, raw)).rowcount
                assert changed == 1, "Chat changed during repair; refusing to overwrite newer messages"
        else:
            artifact = {"file_id": message["files"][-1]["id"], "url": message["files"][-1]["url"]}

        if args.sync:
            from open_webui.models.functions import Functions
            from open_webui.models.tools import Tools
            root = Path(__file__).parent
            assert await Functions.update_function_by_id("owui-main", {
                "content": (root / "native_pipe.py").read_text(encoding="utf-8")}) is not None
            assert await Tools.update_tool_by_id("coding_agent", {
                "content": (root / "coding_tool.py").read_text(encoding="utf-8")}) is not None
        print("ANIMATION_RECOVERED=" + json.dumps({
            "chat_url": "http://127.0.0.1:13000/c/" + CHAT_ID,
            "file_url": "http://127.0.0.1:13000" + artifact["url"],
            "terminal_path": TARGET, "bytes": len(html.encode("utf-8")),
            "sha256": hashlib.sha256(html.encode("utf-8")).hexdigest(),
            "backup": str(backup), "runtime_synced": args.sync}, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
