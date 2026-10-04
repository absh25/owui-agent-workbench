"""Install the single collaboration entry and retire the two known legacy Pipes."""

import asyncio
from datetime import datetime
import json
import os
import sqlite3
from pathlib import Path

from open_webui.models.functions import Functions, FunctionForm
from open_webui.models.models import Models, ModelForm
from open_webui.models.tools import Tools, ToolForm
from open_webui.models.users import Users
from open_webui.models.config import Config
from open_webui.routers.tools import get_tool_specs
from open_webui.utils.plugin import load_tool_module_by_id


ROOT = Path(__file__).parent
MAIN_ID = "owui-main"
MAIN_NAME = "协作助手"
LEGACY_FUNCTION_IDS = ("claude_code", "owui_supervisor")
LEGACY_MODEL_IDS = ("claude_code.claude-code", "owui_supervisor")
REQUIRED_TOOLS = {"coding_agent": "delegate_coding_task",
                  "retrieval_agent": "delegate_retrieval_task"}


def migrate_chat_models(chat):
    """Change only raw-supervisor model metadata, never text or old Claude chats."""
    changed = False

    def update(node):
        nonlocal changed
        if not isinstance(node, dict):
            return
        migrated_node = node.get("model") == "owui_supervisor"
        if migrated_node:
            node["model"] = MAIN_ID
            changed = True
            if isinstance(node.get("modelName"), str):
                node["modelName"] = MAIN_NAME
        if isinstance(node.get("models"), list) and "owui_supervisor" in node["models"]:
            node["models"] = [MAIN_ID if value == "owui_supervisor" else value
                              for value in node["models"]]
            changed = True

    update(chat)
    messages = (chat.get("history") or {}).get("messages") or {}
    if isinstance(messages, dict):
        for message in messages.values():
            update(message)
    if isinstance(chat.get("messages"), list):
        for message in chat["messages"]:
            update(message)
    return changed


def migrate_legacy_chats(database):
    count = 0
    with sqlite3.connect(database) as connection:
        for chat_id, raw in connection.execute("SELECT id, chat FROM chat").fetchall():
            chat = json.loads(raw)
            if migrate_chat_models(chat):
                connection.execute("UPDATE chat SET chat = ? WHERE id = ?",
                                   (json.dumps(chat, ensure_ascii=False), chat_id))
                count += 1
    return count


def registry_counts(database):
    with sqlite3.connect(database) as connection:
        return {"pipes": connection.execute("SELECT count(*) FROM function WHERE type='pipe'").fetchone()[0],
                "models": connection.execute("SELECT count(*) FROM model").fetchone()[0],
                "chats": connection.execute("SELECT count(*) FROM chat").fetchone()[0]}


async def installation_context():
    """Prefer the current entry; old Pipes are migration sources, not dependencies."""
    owner_id, saved = None, {}
    for function_id in (MAIN_ID, "owui_supervisor", "claude_code"):
        function = await Functions.get_function_by_id(function_id)
        if function is not None:
            owner_id = owner_id or function.user_id
            valves = await Functions.get_function_valves_by_id(function_id) or {}
            if not saved and valves.get("MODEL"):
                saved = dict(valves)
    if owner_id is None:
        model = await Models.get_model_by_id(MAIN_ID)
        owner_id = model.user_id if model is not None else None
    if owner_id is None:
        page = await Users.get_users(limit=1000)
        owner_id = next((user.id for user in page["users"] if user.role == "admin"), None)
    if owner_id is None:
        raise RuntimeError("需要现有助手的所有者或管理员账号。")
    saved.setdefault("MODEL", os.environ.get("ANTHROPIC_MODEL", ""))
    if not saved["MODEL"]:
        raise RuntimeError("请配置 ANTHROPIC_MODEL 或保留助手的 MODEL；尚未清理旧入口。")
    return owner_id, saved


async def verify_installation():
    function = await Functions.get_function_by_id(MAIN_ID)
    model = await Models.get_model_by_id(MAIN_ID)
    if function is None or function.type != "pipe" or not function.is_active:
        raise RuntimeError("协作助手 Pipe 未启用；尚未清理旧入口。")
    if model is None or model.base_model_id is not None or not model.is_active:
        raise RuntimeError("协作助手必须直接覆盖同名 Pipe；尚未清理旧入口。")
    tool_ids = set(getattr(model.meta, "toolIds", None) or [])
    if not REQUIRED_TOOLS.keys() <= tool_ids or model.params.function_calling != "native":
        raise RuntimeError("协作助手委派工具或原生循环配置不完整；尚未清理旧入口。")
    if not (await Functions.get_function_valves_by_id(MAIN_ID) or {}).get("MODEL"):
        raise RuntimeError("协作助手模型参数为空；尚未清理旧入口。")
    for tool_id, method in REQUIRED_TOOLS.items():
        tool = await Tools.get_tool_by_id(tool_id)
        if tool is None or method not in {spec.get("name") for spec in tool.specs}:
            raise RuntimeError("委派工具注册不完整：" + tool_id)
        if not (await Tools.get_tool_valves_by_id(tool_id) or {}).get("MODEL"):
            raise RuntimeError("委派工具模型参数为空：" + tool_id)


async def main():
    database = Path("/app/backend/data/webui.db")
    backup = database.with_name("webui.db.bak-before-collaboration-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
    with sqlite3.connect(database) as source, sqlite3.connect(backup) as target:
        source.backup(target)
    backup.chmod(0o600)
    print("BACKUP", backup, flush=True)
    before = registry_counts(database)
    owner_id, saved = await installation_context()
    for tool_id, filename, name, description in (
        ("coding_agent", "coding_tool.py", "编码任务", "通过 Claude Agent SDK 管理编码、调试和测试。"),
        ("retrieval_agent", "retrieval_tool.py", "检索任务", "独立检索 Agent 负责查询、证据核对、重试和停止。"),
    ):
        content = (ROOT / filename).read_text(encoding="utf-8")
        module, manifest = await load_tool_module_by_id(tool_id, content=content)
        specs = get_tool_specs(module)
        for spec in specs:
            params = spec["parameters"]
            params["properties"] = {key: value for key, value in params.get("properties", {}).items()
                                    if not key.startswith("__")}
            params["required"] = [key for key in params.get("required", []) if not key.startswith("__")]
        form = ToolForm(id=tool_id, name=name, content=content,
                        meta={"description": description, "manifest": manifest})
        if await Tools.get_tool_by_id(form.id):
            installed = await Tools.update_tool_by_id(form.id, {"content": content, "specs": specs, "name": form.name})
        else:
            installed = await Tools.insert_new_tool(owner_id, form, specs)
        if installed is None:
            raise RuntimeError(name + "注册失败。")
        valves = await Tools.get_tool_valves_by_id(form.id) or {}
        valves.setdefault("MODEL", saved["MODEL"])
        await Tools.update_tool_valves_by_id(form.id, valves)

    content = (ROOT / "native_pipe.py").read_text(encoding="utf-8")
    function_form = FunctionForm(id=MAIN_ID, name=MAIN_NAME, content=content,
                                meta={"description": "规划、知识检索与编码协作，业务提交由用户确认。"})
    if await Functions.get_function_by_id(MAIN_ID):
        installed = await Functions.update_function_by_id(MAIN_ID, {"content": content,
                                            "name": MAIN_NAME, "is_active": True})
    else:
        installed = await Functions.insert_new_function(owner_id, "pipe", function_form)
        await Functions.update_function_by_id(MAIN_ID, {"is_active": True})
    if installed is None:
        raise RuntimeError("协作助手注册失败。")
    await Functions.update_function_valves_by_id(MAIN_ID, saved)

    form = ModelForm(id=MAIN_ID, base_model_id=None, name=MAIN_NAME, is_active=True,
        meta={"description": "规划、知识检索与编码协作，业务提交由用户确认。",
              "toolIds": ["coding_agent", "retrieval_agent", "server:mcp:company-jira", "server:mcp:local-weather"],
              "capabilities": {"terminal": False}}, params={"function_calling": "native"})
    form.meta.builtinTools = {"knowledge": False, "subagents": False, "code_interpreter": False}
    existing = await Models.get_model_by_id(MAIN_ID)
    if existing:
        updated = ModelForm(**existing.model_dump())
        tool_ids = [tool for tool in (getattr(updated.meta, "toolIds", None) or [])
                    if not tool.startswith("server:mcp:ragflow-knowledge")]
        updated.meta.toolIds = list(dict.fromkeys(tool_ids + ["retrieval_agent", "coding_agent"]))
        updated.meta.description = form.meta.description
        updated.meta.capabilities = {**(updated.meta.capabilities or {}), "terminal": False}
        updated.meta.builtinTools = {**(getattr(updated.meta, "builtinTools", None) or {}),
                                    **form.meta.builtinTools}
        updated.name, updated.base_model_id, updated.is_active = MAIN_NAME, None, True
        updated.params.function_calling = "native"
        if await Models.update_model_by_id(MAIN_ID, updated) is None:
            raise RuntimeError("协作助手工具绑定失败。")
    elif await Models.insert_new_model(form, owner_id) is None:
        raise RuntimeError("协作助手模型注册失败。")

    # Validate the replacement before retiring only the two known legacy entries.
    await verify_installation()
    migrated = migrate_legacy_chats(database)
    deleted_functions, deleted_models = [], []
    for function_id in LEGACY_FUNCTION_IDS:
        if await Functions.get_function_by_id(function_id):
            if not await Functions.delete_function_by_id(function_id):
                raise RuntimeError("无法清理旧 Pipe：" + function_id)
            deleted_functions.append(function_id)
    for model_id in LEGACY_MODEL_IDS:
        if await Models.get_model_by_id(model_id):
            if not await Models.delete_model_by_id(model_id):
                raise RuntimeError("无法清理旧模型：" + model_id)
            deleted_models.append(model_id)
    await verify_installation()
    # Existing persisted OWUI settings take precedence over the environment.
    await Config.upsert({"evaluation.arena.enable": False})
    after = registry_counts(database)
    if before["chats"] != after["chats"]:
        raise RuntimeError("会话数量异常，请检查备份；注册不应删除会话。")
    print("INSTALLED", MAIN_ID, MAIN_NAME, "coding_agent", "retrieval_agent", "MODEL", saved["MODEL"])
    print("CLEANUP", json.dumps({"before": before, "after": after,
        "deleted_pipes": deleted_functions, "deleted_models": deleted_models,
        "migrated_supervisor_chats": migrated}, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
