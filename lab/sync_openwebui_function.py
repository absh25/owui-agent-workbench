"""Safely sync the mounted Claude Pipe into Open WebUI's SQLite database."""

import json
import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path


DB = Path("/app/backend/data/webui.db")
SOURCE = Path("/lab/claude_agent_pipe.py")
FUNCTION_ID = "claude_code"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--restore-memory-tools", action="store_true")
    parser.add_argument("--model", help="Explicitly switch the saved Pipe model")
    parser.add_argument("--inherit-api-credentials", action="store_true",
                        help="Clear saved API/OAuth overrides; use container environment")
    args = parser.parse_args()
    apply = args.apply
    connection = sqlite3.connect(DB)
    columns = [row[1] for row in connection.execute('PRAGMA table_info("function")')]
    row = connection.execute(
        'SELECT * FROM "function" WHERE id = ?', (FUNCTION_ID,)
    ).fetchone()
    if row is None:
        raise SystemExit(f"Function {FUNCTION_ID!r} does not exist")
    print("FUNCTION_COLUMNS", columns)
    print("MODE", "apply" if apply else "dry-run")
    if not apply:
        record = dict(zip(columns, row))
        valves = json.loads(record.get("valves") or "{}")
        print("CONTENT_MATCH", record.get("content") == SOURCE.read_text(encoding="utf-8"))
        print("MODEL", valves.get("MODEL"))
        print("TOOL_MODE", valves.get("TOOL_MODE"))
        return

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = DB.with_name(f"webui.db.bak-before-owui-tool-bridge-{timestamp}")
    # The running OWUI process uses WAL; a filesystem copy of only webui.db
    # can miss committed rows. SQLite's online backup creates a consistent
    # snapshot without stopping the service.
    with sqlite3.connect(backup) as backup_connection:
        connection.backup(backup_connection)

    assignments = ["content = ?"]
    values = [SOURCE.read_text(encoding="utf-8")]
    if "updated_at" in columns:
        assignments.append("updated_at = ?")
        values.append(int(datetime.now().timestamp()))
    values.append(FUNCTION_ID)
    connection.execute(
        f'UPDATE "function" SET {", ".join(assignments)} WHERE id = ?', values
    )

    # Existing saved valves override new code defaults. Update only the stale
    # model and add the strict tool mode while preserving every other setting.
    if "valves" in columns:
        current = connection.execute(
            'SELECT valves FROM "function" WHERE id = ?', (FUNCTION_ID,)
        ).fetchone()[0]
        valves = json.loads(current) if current else {}
        if valves.get("MODEL") in {
            None,
            "",
            "deepseek-flash",
            "DeepSeek-V4-Flash",
        }:
            valves["MODEL"] = "deepseek-ai/DeepSeek-V4-Flash"
        valves["TOOL_MODE"] = "owui_only"
        if args.model:
            valves["MODEL"] = args.model
        if args.inherit_api_credentials:
            valves["ANTHROPIC_API_KEY"] = ""
            valves["CLAUDE_CODE_OAUTH_TOKEN"] = ""
        if args.restore_memory_tools:
            memory_tools = {
                "search_memories", "list_memory_paths", "read_memory_path", "list_memories",
                "update_memory", "add_memory", "replace_memory_content", "delete_memory",
            }
            existing_builtin_tools = {
                item.strip() for item in str(valves.get("OWUI_BUILTIN_TOOLS") or "").split(",")
                if item.strip()
            }
            valves["OWUI_BUILTIN_TOOLS"] = ",".join(sorted(existing_builtin_tools | memory_tools))
        connection.execute(
            'UPDATE "function" SET valves = ? WHERE id = ?',
            (json.dumps(valves, ensure_ascii=False), FUNCTION_ID),
        )

    connection.commit()
    connection.close()
    print("BACKUP", backup)
    print("SYNCED", FUNCTION_ID, len(values[0]), "characters")


if __name__ == "__main__":
    main()
