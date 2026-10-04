"""Check model-metadata migration without touching the live registry."""

import copy
import json
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from supervisor.register import migrate_chat_models, migrate_legacy_chats, installation_context


class ChatMigrationTests(unittest.TestCase):
    def test_migrate_metadata_preserves_content_and_other_models(self):
        chat = {"models": ["owui_supervisor", "claude_code.claude-code"],
                "title": "owui_supervisor conversation",
                "history": {"messages": {
                    "user": {"models": ["owui_supervisor"], "content": "owui_supervisor is literal text"},
                    "assistant": {"model": "owui_supervisor", "modelName": "OWUI 主助手（兼容入口）",
                                  "content": "unchanged answer", "output": [{"model": "owui_supervisor"}]},
                    "legacy": {"model": "claude_code.claude-code", "content": "keep old Claude"}}},
                "messages": [{"model": "owui_supervisor", "content": "same answer"}]}
        original = copy.deepcopy(chat)
        self.assertTrue(migrate_chat_models(chat))
        self.assertEqual(chat["models"], ["owui-main", "claude_code.claude-code"])
        self.assertEqual(chat["history"]["messages"]["assistant"]["modelName"], "协作助手")
        self.assertEqual(chat["messages"][0]["model"], "owui-main")
        self.assertEqual(chat["title"], original["title"])
        for key, message in original["history"]["messages"].items():
            self.assertEqual(chat["history"]["messages"][key]["content"], message["content"])
        self.assertEqual(chat["history"]["messages"]["assistant"]["output"],
                         original["history"]["messages"]["assistant"]["output"])
        self.assertEqual(chat["history"]["messages"]["legacy"], original["history"]["messages"]["legacy"])
        self.assertFalse(migrate_chat_models(chat))

    def test_old_claude_chat_is_unchanged(self):
        chat = {"models": ["claude_code.claude-code"], "history": {"messages": {
            "one": {"model": "claude_code.claude-code", "content": "owui_supervisor"}}}}
        original = copy.deepcopy(chat)
        self.assertFalse(migrate_chat_models(chat))
        self.assertEqual(chat, original)

    def test_database_migration_only_rewrites_referenced_chats_and_is_idempotent(self):
        with tempfile.NamedTemporaryFile(suffix=".sqlite3") as file:
            connection = sqlite3.connect(file.name)
            connection.execute("CREATE TABLE chat(id TEXT, chat TEXT)")
            legacy = json.dumps({"models": ["owui_supervisor"], "messages": [{"content": "keep"}]})
            untouched = '{"models": ["claude_code.claude-code"], "messages": []}'
            connection.executemany("INSERT INTO chat VALUES (?, ?)", [("raw", legacy), ("old", untouched)])
            connection.commit()
            self.assertEqual(migrate_legacy_chats(file.name), 1)
            rows = dict(connection.execute("SELECT id, chat FROM chat"))
            self.assertEqual(rows["old"], untouched)
            self.assertEqual(json.loads(rows["raw"])["messages"][0]["content"], "keep")
            self.assertEqual(migrate_legacy_chats(file.name), 0)
            self.assertEqual(connection.execute("SELECT count(*) FROM chat").fetchone()[0], 2)
            connection.close()


class RegistrationContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_repeated_registration_needs_no_legacy_pipe(self):
        async def function(function_id):
            return SimpleNamespace(user_id="current-owner") if function_id == "owui-main" else None
        with patch("supervisor.register.Functions.get_function_by_id", side_effect=function), \
             patch("supervisor.register.Functions.get_function_valves_by_id", new=AsyncMock(return_value={
                 "MODEL": "configured-model", "MAX_TOKENS": 4096})), \
             patch("supervisor.register.Users.get_users", new=AsyncMock()) as users:
            owner, valves = await installation_context()
        self.assertEqual(owner, "current-owner")
        self.assertEqual(valves, {"MODEL": "configured-model", "MAX_TOKENS": 4096})
        users.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
