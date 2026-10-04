"""Register the read-only MCP for the existing OWUI workspace owner."""

import asyncio
from datetime import datetime
import os
from pathlib import Path
import sqlite3

from open_webui.models.config import Config
from open_webui.models.functions import Functions
from open_webui.models.models import Models, ModelForm
from open_webui.models.tools import Tools


async def main():
    token = os.environ.get("RAGFLOW_MCP_TOKEN", "")
    function = await Functions.get_function_by_id("owui-main")
    if not token or function is None:
        raise RuntimeError("需要已安装的协作助手和 RAGFLOW_MCP_TOKEN；先运行 /lab/supervisor/register.py。")
    database = Path("/app/backend/data/webui.db")
    backup = database.with_name("webui.db.bak-before-ragflow-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
    with sqlite3.connect(database) as source, sqlite3.connect(backup) as destination:
        source.backup(destination)
    backup.chmod(0o600)

    server_id = "ragflow-knowledge"
    connection = {"url": "http://retrieval-lab:8000/mcp", "path": "", "type": "mcp",
        "auth_type": "bearer", "key": token, "forward_cookies": False, "headers": {},
        "config": {"enable": True, "access_grants": [
            {"principal_type": "user", "principal_id": function.user_id, "permission": "read"}]},
        "info": {"id": server_id, "name": "ragflow 频谱感知知识库",
                 "description": "百炼向量检索与原文证据查看；只读 MCP，上传在本地知识库平台完成。"}}
    connections = await Config.get("tool_server.connections", []) or []
    updated = [c for c in connections if (c.get("info") or {}).get("id") != server_id] + [connection]
    await Config.upsert({"tool_server.connections": updated})

    model = await Models.get_model_by_id("owui-main")
    if model is None:
        raise RuntimeError("协作助手模型不存在。")
    form = ModelForm(**model.model_dump())
    if await Tools.get_tool_by_id("retrieval_agent") is None:
        raise RuntimeError("请先运行 /lab/supervisor/register.py 安装独立检索 Agent。")
    tool_ids = [t for t in (getattr(form.meta, "toolIds", None) or [])
                if not t.startswith("server:mcp:" + server_id)]
    if "retrieval_agent" not in tool_ids:
        tool_ids.append("retrieval_agent")
    form.meta.toolIds = tool_ids
    if await Models.update_model_by_id(model.id, form) is None:
        raise RuntimeError("无法绑定知识库工具。")
    content = Path("/lab/supervisor/native_pipe.py").read_text(encoding="utf-8")
    if await Functions.update_function_by_id(function.id, {"content": content}) is None:
        raise RuntimeError("无法同步证据核查提示。")
    print("REGISTERED", server_id, "MODEL", model.id)
    print("BACKUP", backup)


if __name__ == "__main__":
    asyncio.run(main())
