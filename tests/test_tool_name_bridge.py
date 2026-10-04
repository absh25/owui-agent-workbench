"""Provider compatibility must not expand the request's tool permissions."""
import json
import os
import unittest
from unittest.mock import patch

import test_prompt_routing  # Supplies SDK stubs when running outside OWUI.
from claude_agent_pipe import _tool_aliases, _normalize_tool_event, _sdk_tool_name_bridge, _build_owui_mcp_server
from aiohttp import ClientSession, web


class ToolNameTests(unittest.TestCase):
    def test_aliases_only_authorized_tools(self):
        aliases = _tool_aliases(["mcp__openwebui__run_command", "mcp__other__delete"])
        self.assertEqual(aliases, {"run_command": "mcp__openwebui__run_command"})
        event = {"content_block": {"type": "tool_use", "name": "run_command",
                                   "input": {"command": "echo run_command"}}}
        self.assertTrue(_normalize_tool_event(event, aliases))
        self.assertEqual(event["content_block"]["name"], "mcp__openwebui__run_command")
        self.assertEqual(event["content_block"]["input"]["command"], "echo run_command")
        unknown = {"content_block": {"type": "tool_use", "name": "delete"}}
        self.assertFalse(_normalize_tool_event(unknown, aliases))


class BridgeHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def call_adapter(self, payload, *, tool_name="run_command", tool_type="terminal", arguments=None):
        calls = []

        async def original(**kwargs):
            calls.append(kwargs)
            return (payload, {"private-header": "not model evidence"})

        entry = {"type": tool_type, "callable": original, "spec": {"parameters": {
            "type": "object", "properties": {"command": {}, "cwd": {}, "wait": {}},
        }}}
        with patch("claude_agent_pipe.tool", lambda *a: lambda f: f), \
             patch("claude_agent_pipe.create_sdk_mcp_server", lambda *a, **kw: kw["tools"]):
            handlers, _, _ = _build_owui_mcp_server({tool_name: entry})
            result = await handlers[0](arguments or {"command": "pwd"})
        self.assertNotIn("private-header", str(result))
        return result, calls

    async def test_terminal_nonzero_exit_is_an_agent_error(self):
        for exit_code in (1, 127, "2"):
            with self.subTest(exit_code=exit_code):
                result, _ = await self.call_adapter({"status": "done", "exit_code": exit_code})
                self.assertTrue(result.get("isError"))
        for payload in ({"status": "done", "exit_code": 0},
                        {"status": "running", "exit_code": None},
                        {"status": "running", "id": "background-process"}):
            with self.subTest(payload=payload):
                result, _ = await self.call_adapter(payload)
                self.assertFalse(result.get("isError", False))

    async def test_explicit_error_flags_are_preserved(self):
        for flag in ("error", "isError", "is_error"):
            with self.subTest(flag=flag):
                result, _ = await self.call_adapter({flag: True}, tool_type="mcp")
                self.assertTrue(result.get("isError"))

    async def test_backend_cwd_correction_only_applies_to_terminal(self):
        for cwd in ("/app/backend", "/app/backend/data/claude-agent-workspaces/chat"):
            with self.subTest(cwd=cwd):
                _, calls = await self.call_adapter({"exit_code": 0}, arguments={"command": "pwd", "cwd": cwd})
                self.assertEqual(calls, [{"command": "pwd", "wait": "10"}])
        for tool_type, cwd in (("terminal", "/workspace/custom"), ("mcp", "/app/backend"),
                               ("mcp", "/app/backend/data/workspace")):
            with self.subTest(tool_type=tool_type, cwd=cwd):
                _, calls = await self.call_adapter({"exit_code": 0}, tool_type=tool_type,
                    arguments={"command": "pwd", "cwd": cwd, "wait": "0"})
                self.assertEqual(calls, [{"command": "pwd", "cwd": cwd, "wait": "0"}])
        _, calls = await self.call_adapter({}, tool_type="mcp", arguments={"command": "pwd"})
        self.assertEqual(calls, [{"command": "pwd"}])

    async def test_owui_tuple_error_is_not_reported_as_success(self):
        async def original():
            return ({"error": "Directory not found"}, {"private-header": "not model evidence"})
        entry = {"callable": original, "spec": {"parameters": {"type": "object", "properties": {}}}}
        with patch("claude_agent_pipe.tool", lambda *a: lambda f: f), \
             patch("claude_agent_pipe.create_sdk_mcp_server", lambda *a, **kw: kw["tools"]):
            handlers, _, _ = _build_owui_mcp_server({"list_files": entry})
            result = await handlers[0]({})
        self.assertTrue(result["isError"])
        self.assertIn("Directory not found", str(result))
        self.assertNotIn("private-header", str(result))

    async def test_sse_normalization_and_authentication(self):
        seen = []

        async def upstream(request):
            seen.append(request.headers.get("x-api-key"))
            output = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await output.prepare(request)
            event = {"type": "content_block_start", "index": 0,
                     "content_block": {"type": "tool_use", "id": "test",
                                       "name": "run_command", "input": {}}}
            raw = ("event: content_block_start\ndata: " + json.dumps(event) + "\n\n").encode()
            # Force transport fragmentation: the bridge must buffer full lines.
            await output.write(raw[:37])
            await output.write(raw[37:])
            await output.write_eof()
            return output

        app = web.Application()
        app.router.add_post("/v1/messages", upstream)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        try:
            with patch.dict(os.environ, {"ANTHROPIC_BASE_URL": f"http://127.0.0.1:{port}",
                                         "ANTHROPIC_API_KEY": "synthetic-key"}):
                options = {}
                async with _sdk_tool_name_bridge(options, ["mcp__openwebui__run_command"]):
                    env = options["env"]
                    async with ClientSession() as client:
                        url = env["ANTHROPIC_BASE_URL"] + "/v1/messages"
                        async with client.post(url, json={}) as response:
                            self.assertEqual(response.status, 401)
                        headers = {"x-api-key": env["ANTHROPIC_API_KEY"]}
                        for tools, expected in [
                            ([{"name": "mcp__openwebui__run_command"}], "mcp__openwebui__run_command"),
                            ([], "run_command"),
                        ]:
                            async with client.post(url, json={"tools": tools}, headers=headers) as response:
                                self.assertEqual(response.status, 200)
                                text = await response.text()
                                data = next(line[6:] for line in text.splitlines() if line.startswith("data: "))
                                self.assertEqual(json.loads(data)["content_block"]["name"], expected)
                self.assertEqual(seen, ["synthetic-key", "synthetic-key"])
        finally:
            await runner.cleanup()
