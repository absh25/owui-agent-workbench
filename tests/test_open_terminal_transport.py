"""Exercise the remote SDK protocol with a real WS endpoint, without a model."""

from __future__ import annotations

import asyncio
import json
import sys
import types
import unittest
from abc import ABC, abstractmethod
from types import SimpleNamespace

from aiohttp import ClientSession, web


# The host test suite already supplies message stubs when the SDK is absent.
# Supply only the newly needed internal interfaces, preserving those stubs.
try:
    from claude_agent_sdk._internal.transport import Transport  # noqa: F401
except (ModuleNotFoundError, ImportError):
    try:
        import test_prompt_routing  # noqa: F401
    except ModuleNotFoundError:
        from tests import test_prompt_routing  # noqa: F401

    class Transport(ABC):
        @abstractmethod
        async def connect(self):
            pass

        @abstractmethod
        async def write(self, data):
            pass

        @abstractmethod
        def read_messages(self):
            pass

        @abstractmethod
        async def close(self):
            pass

        @abstractmethod
        def is_ready(self):
            pass

        @abstractmethod
        async def end_input(self):
            pass

    class CLIConnectionError(Exception):
        pass

    class ProcessError(Exception):
        def __init__(self, message, exit_code=None, stderr=None):
            super().__init__(message)
            self.exit_code = exit_code
            self.stderr = stderr

    internal = types.ModuleType("claude_agent_sdk._internal")
    transport_module = types.ModuleType("claude_agent_sdk._internal.transport")
    transport_module.Transport = Transport
    errors = types.ModuleType("claude_agent_sdk._errors")
    errors.CLIConnectionError = CLIConnectionError
    errors.ProcessError = ProcessError
    sys.modules["claude_agent_sdk._internal"] = internal
    sys.modules["claude_agent_sdk._internal.transport"] = transport_module
    sys.modules["claude_agent_sdk._errors"] = errors

from runtime.open_terminal_transport import OpenTerminalTransport, _remote_options
from claude_agent_sdk._errors import CLIConnectionError, ProcessError


_TOKEN = "ot-sdk-" + "a" * 32


class RemoteOptionsTests(unittest.TestCase):
    def test_sdk_mcp_objects_and_python_callbacks_stay_local(self):
        options = SimpleNamespace(
            model="test-model", tools=["Bash", "Write"], cwd="/workspace/alice",
            system_prompt="Write inside the sandbox", include_partial_messages=True,
            mcp_servers={"openwebui": {"type": "sdk", "name": "openwebui", "instance": object()}},
            stderr=lambda line: None, hooks={"PreToolUse": object()}, env={"DISABLE_TELEMETRY": "1"},
        )
        serialized = _remote_options(options, proxy_url="http://open-webui:12345", proxy_token=_TOKEN,
                                     config_directory="/workspace/alice/.claude-agent-sessions/chat")
        self.assertEqual(serialized["mcp_servers"], {"openwebui": {"type": "sdk", "name": "openwebui"}})
        self.assertNotIn("stderr", serialized)
        self.assertNotIn("hooks", serialized)
        self.assertEqual(serialized["env"], {
            "ANTHROPIC_BASE_URL": "http://open-webui:12345", "ANTHROPIC_API_KEY": _TOKEN,
            "CLAUDE_CONFIG_DIR": "/workspace/alice/.claude-agent-sessions/chat",
        })

    def test_long_lived_provider_credentials_are_rejected(self):
        for key in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                _remote_options({"env": {key: "upstream-private-secret"}},
                                proxy_url="http://open-webui:1234", proxy_token=_TOKEN, config_directory=None)
        with self.assertRaises(ValueError):
            OpenTerminalTransport("http://terminal", "terminal-key", {}, proxy_url="http://open-webui:1234", proxy_token="real-provider-key")

    def test_local_files_external_mcp_and_cli_arguments_are_rejected(self):
        for options in (
            {"system_prompt": {"type": "file", "path": "C:/private/prompt.txt"}},
            {"mcp_servers": {"untrusted": {"type": "stdio", "command": "shell"}}},
            {"extra_args": {"mcp-config": "C:/private/mcp.json"}},
            {"settings": "C:/private/settings.json"},
        ):
            with self.subTest(options=options), self.assertRaises(ValueError):
                _remote_options(options, proxy_url=None, proxy_token=None, config_directory=None)
        with self.assertRaises(ValueError):
            OpenTerminalTransport("http://terminal", "key", {}, session_headers={"Authorization": "other-key"})


class WebSocketTransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.received = []
        self.auth = []
        self.connections = []
        self.mode = "normal"

        async def endpoint(request):
            self.auth.append(dict(request.headers))
            socket = web.WebSocketResponse()
            await socket.prepare(request)
            self.connections.append(socket)
            self.received.append(await socket.receive_json())
            if self.mode == "start-error":
                await socket.send_json({"type": "error", "message": "CLI missing"})
                await socket.close()
                return socket
            if self.mode != "no-ready":
                await socket.send_json({"type": "ready", "pid": 42, "cwd": "/workspace/alice"})
            async for frame in socket:
                event = json.loads(frame.data)
                self.received.append(event)
                if event["type"] == "stdin":
                    if self.mode == "bad-stdout":
                        await socket.send_json({"type": "stdout", "data": "invalid"})
                    elif self.mode == "unexpected-close":
                        await socket.close()
                    elif self.mode == "silent":
                        continue
                    else:
                        # The SDK control request must survive unchanged so
                        # OWUI can execute its local MCP handlers and reply.
                        await socket.send_json({"type": "stdout", "data": {
                            "type": "control_request", "request_id": "r1", "request": {
                                "subtype": "mcp_message", "server_name": "openwebui",
                                "message": {"jsonrpc": "2.0", "id": 9, "method": "tools/call"},
                            },
                        }})
                        await socket.send_json({"type": "stderr", "data": "diagnostic only"})
                elif event["type"] == "end_input":
                    await socket.send_json({"type": "exit", "code": 7 if self.mode == "exit-error" else 0})
                    await socket.close()
                elif event["type"] == "close":
                    await socket.close()
            return socket

        app = web.Application()
        app.router.add_get("/claude-sdk", endpoint)
        self.runner = web.AppRunner(app, shutdown_timeout=0.1)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.base = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
        self.transports = []

    async def asyncTearDown(self):
        for transport in self.transports:
            await transport.close()
        for connection in self.connections:
            await connection.close()
        await self.runner.cleanup()

    def transport(self, **kwargs):
        transport = OpenTerminalTransport(self.base, "selected-terminal-key", kwargs.pop("options", {}), **kwargs)
        self.transports.append(transport)
        return transport

    async def test_raw_control_protocol_and_shutdown(self):
        diagnostics = []
        transport = self.transport(options={"stderr": diagnostics.append}, session_headers={"X-User-Id": "alice"})
        await transport.connect()
        self.assertTrue(transport.is_ready())
        self.assertEqual(transport.status()["pid"], 42)
        raw = '{"type":"control_response","response":{"request_id":"r1","response":{"result":"工具结果"}}}\n'
        await transport.write(raw)
        await transport.end_input()
        await transport.end_input()
        messages = [message async for message in transport.read_messages()]
        self.assertEqual(messages[0]["request"]["message"]["method"], "tools/call")
        self.assertEqual(self.received[1], {"type": "stdin", "data": raw})
        self.assertEqual(sum(event["type"] == "end_input" for event in self.received), 1)
        self.assertEqual(self.auth[0]["Authorization"], "Bearer selected-terminal-key")
        self.assertEqual(self.auth[0]["X-User-Id"], "alice")
        self.assertEqual(diagnostics, ["diagnostic only"])
        self.assertEqual(transport.status()["exit_code"], 0)
        self.assertFalse(transport.is_ready())

    async def test_nonzero_exit_is_a_process_error_after_prior_messages(self):
        self.mode = "exit-error"
        transport = self.transport()
        await transport.connect()
        await transport.write('{"type":"user"}\n')
        await transport.end_input()
        iterator = transport.read_messages()
        self.assertEqual((await anext(iterator))["type"], "control_request")
        with self.assertRaises(ProcessError) as failure:
            await anext(iterator)
        self.assertEqual(failure.exception.exit_code, 7)

    async def test_malformed_stdout_and_abrupt_disconnect_surface_errors(self):
        for mode in ("bad-stdout", "unexpected-close"):
            with self.subTest(mode=mode):
                self.mode = mode
                transport = self.transport()
                await transport.connect()
                await transport.write('{}\n')
                with self.assertRaises(CLIConnectionError):
                    await anext(transport.read_messages())
                await transport.close()

    async def test_startup_failure_and_timeout_close_owned_sessions(self):
        for mode, error in (("start-error", CLIConnectionError), ("no-ready", TimeoutError)):
            with self.subTest(mode=mode):
                self.mode = mode
                transport = self.transport(connect_timeout=0.05)
                with self.assertRaises(error):
                    await transport.connect()
                self.assertTrue(transport._session.closed)
                self.assertFalse(transport.is_ready())

    async def test_cancel_stops_silent_runtime_and_unblocks_sdk_reader(self):
        self.mode = "silent"
        for action in ("cancel", "close"):
            with self.subTest(action=action):
                close_count = sum(event["type"] == "close" for event in self.received)
                transport = self.transport()
                await transport.connect()
                await transport.write('{}\n')
                reader = asyncio.create_task(anext(transport.read_messages(), None))
                await getattr(transport, action)()
                self.assertIsNone(await asyncio.wait_for(reader, 1))
                self.assertTrue(transport._session.closed)
                self.assertFalse(transport.is_ready())
                await transport.close()
                await transport.cancel()
                self.assertEqual(sum(event["type"] == "close" for event in self.received), close_count + 1)
                with self.assertRaises(CLIConnectionError):
                    await transport.write('{}\n')

    async def test_supplied_http_session_is_not_closed(self):
        async with ClientSession() as session:
            transport = self.transport(session=session)
            await transport.connect()
            await transport.close()
            self.assertFalse(session.closed)


if __name__ == "__main__":
    unittest.main()
