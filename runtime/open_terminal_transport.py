"""Keep the Agent SDK in OWUI while its Claude process runs in OpenTerminal.

This transports the SDK's complete bidirectional NDJSON/control protocol. It
does not replace the SDK query loop or execute OWUI tools in the container.
Only explicitly supplied, request-scoped proxy credentials reach the runtime.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import AsyncIterator, Mapping
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import aiohttp

from claude_agent_sdk._errors import CLIConnectionError, ProcessError
from claude_agent_sdk._internal.transport import Transport


_OPTION_FIELDS = (
    "tools", "allowed_tools", "disallowed_tools", "system_prompt",
    "strict_mcp_config", "permission_mode", "continue_conversation", "resume",
    "session_id", "max_turns", "max_budget_usd", "model", "fallback_model",
    "betas", "permission_prompt_tool_name", "cwd", "include_partial_messages",
    "include_hook_events", "fork_session", "resume_session_at", "resume_drops_turn",
    "setting_sources", "max_thinking_tokens", "thinking", "effort",
    "output_format", "task_budget",
)
_END = object()


def _option(options: Any, name: str, default: Any = None) -> Any:
    return options.get(name, default) if isinstance(options, Mapping) else getattr(options, name, default)


def _remote_options(
    options: Any,
    *,
    proxy_url: str | None,
    proxy_token: str | None,
    config_directory: str | None,
) -> dict[str, Any]:
    """Serialize CLI options, leaving Python handlers and SDK MCPs in OWUI."""
    result: dict[str, Any] = {}
    for name in _OPTION_FIELDS:
        value = _option(options, name)
        if value is not None:
            result[name] = str(value) if name == "cwd" else value

    prompt = result.get("system_prompt")
    if isinstance(prompt, Mapping) and prompt.get("type") == "file":
        raise ValueError("Remote runtime requires an inline system prompt, not a local file")
    if _option(options, "extra_args") or _option(options, "plugins") or _option(options, "settings"):
        raise ValueError("Local settings, plugins and arbitrary CLI arguments cannot be forwarded")

    servers = _option(options, "mcp_servers") or {}
    if not isinstance(servers, Mapping):
        raise ValueError("Remote MCP configuration must contain named SDK servers")
    named_servers = {}
    for name, server in servers.items():
        if not isinstance(name, str) or not isinstance(server, Mapping) or server.get("type") != "sdk":
            raise ValueError("Only in-process SDK MCP server names may cross the transport")
        named_servers[name] = {"type": "sdk", "name": name}
    if named_servers:
        result["mcp_servers"] = named_servers

    env: dict[str, str] = {}
    if bool(proxy_url) != bool(proxy_token):
        raise ValueError("A request-scoped proxy URL and token must be supplied together")
    if proxy_url and proxy_token:
        if not proxy_token.startswith("ot-sdk-") or len(proxy_token) < len("ot-sdk-") + 32:
            raise ValueError("Remote model credentials must be a scoped ot-sdk proxy token")
        target = urlsplit(proxy_url)
        if target.scheme not in {"http", "https"} or not target.hostname or target.username or target.password or target.query or target.fragment:
            raise ValueError("Invalid request-scoped model proxy URL")
        env.update(ANTHROPIC_BASE_URL=proxy_url, ANTHROPIC_API_KEY=proxy_token)
    if config_directory is not None:
        path = PurePosixPath(config_directory)
        if not path.is_absolute() or ".." in path.parts or "\\" in config_directory:
            raise ValueError("Claude configuration directory must be an absolute sandbox path")
        env["CLAUDE_CONFIG_DIR"] = str(path)

    # Even when the normal options contain credentials, never inherit them.
    # Matching explicitly supplied proxy values are harmless and are rebuilt
    # from the constructor arguments above, rather than copied from options.
    source_env = _option(options, "env") or {}
    for name, value in source_env.items():
        if name in {"ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN"} and value and value != env.get(name):
            raise ValueError("Provider credentials must remain in OWUI; supply a scoped proxy token")
    if env:
        result["env"] = env
    # Copy JSON values without traversing SDK server instances or callbacks.
    return json.loads(json.dumps(result, ensure_ascii=False))


class OpenTerminalTransport(Transport):
    """Agent SDK transport for an authenticated OpenTerminal /claude-sdk WS.

    ``options`` is the original ClaudeAgentOptions or equivalent mapping.
    ``proxy_token`` must be a temporary token for OWUI's model proxy, never an
    upstream provider credential. ``config_directory`` is inside OpenTerminal.
    ``session_headers`` retains the selected terminal's user/session routing.
    """

    def __init__(
        self,
        base_url: str,
        terminal_api_key: str,
        options: Any,
        *,
        proxy_url: str | None = None,
        proxy_token: str | None = None,
        config_directory: str | None = None,
        session_headers: Mapping[str, str] | None = None,
        connect_timeout: float = 30,
        cleanup_timeout: float = 3,
        session: aiohttp.ClientSession | None = None,
    ):
        url = urlsplit(base_url.rstrip("/"))
        if url.scheme not in {"http", "https", "ws", "wss"} or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError("Invalid OpenTerminal URL")
        scheme = {"http": "ws", "https": "wss"}.get(url.scheme, url.scheme)
        self._url = urlunsplit((scheme, url.netloc, url.path.rstrip("/") + "/claude-sdk", "", ""))
        self._headers = {"Authorization": f"Bearer {terminal_api_key}"}
        for name, value in (session_headers or {}).items():
            if name.lower() not in {"x-user-id", "x-session-id"}:
                raise ValueError("Only terminal user and session headers may be forwarded")
            self._headers[name] = value
        self._options = _remote_options(options, proxy_url=proxy_url, proxy_token=proxy_token, config_directory=config_directory)
        self._stderr = _option(options, "stderr")
        self._connect_timeout = connect_timeout
        self._cleanup_timeout = cleanup_timeout
        self._session = session
        self._owns_session = session is None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._reader: asyncio.Task | None = None
        self._messages: asyncio.Queue = asyncio.Queue(maxsize=256)
        self._write_lock = asyncio.Lock()
        self._ready = False
        self._closed = False
        self._input_ended = False
        self._exit_code: int | None = None
        self._diagnostics: dict[str, Any] = {}
        self._read_started = False
        self._cleanup_task: asyncio.Task | None = None

    def is_ready(self) -> bool:
        return self._ready and not self._closed and self._ws is not None and not self._ws.closed

    def status(self) -> dict[str, Any]:
        """Runtime diagnostics for OWUI, separate from model messages."""
        return {"ready": self.is_ready(), "input_ended": self._input_ended,
                "exit_code": self._exit_code, **self._diagnostics}

    async def _frame(self) -> dict[str, Any]:
        assert self._ws is not None
        incoming = await self._ws.receive()
        if incoming.type != aiohttp.WSMsgType.TEXT:
            raise CLIConnectionError("OpenTerminal runtime connection closed before an exit event")
        try:
            payload = json.loads(incoming.data)
        except (ValueError, TypeError) as exc:
            raise CLIConnectionError("Invalid JSON from OpenTerminal runtime") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("type"), str):
            raise CLIConnectionError("Invalid OpenTerminal runtime envelope")
        return payload

    async def _emit_stderr(self, data: Any) -> None:
        if not isinstance(data, str):
            raise CLIConnectionError("Invalid stderr envelope from OpenTerminal runtime")
        if self._stderr is not None:
            value = self._stderr(data)
            if inspect.isawaitable(value):
                await value

    async def connect(self) -> None:
        if self.is_ready():
            return
        if self._closed:
            raise CLIConnectionError("OpenTerminal transport is closed")
        try:
            async with asyncio.timeout(self._connect_timeout):
                if self._session is None:
                    self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, sock_connect=self._connect_timeout))
                self._ws = await self._session.ws_connect(self._url, headers=self._headers, max_msg_size=16 * 1024 * 1024, heartbeat=20)
                await self._ws.send_json({"type": "start", "options": self._options})
                while True:
                    event = await self._frame()
                    if event["type"] == "ready":
                        self._diagnostics = {key: event[key] for key in ("pid", "cwd") if key in event}
                        self._ready = True
                        self._reader = asyncio.create_task(self._receive())
                        return
                    if event["type"] == "stderr":
                        await self._emit_stderr(event.get("data"))
                        continue
                    if event["type"] == "error":
                        raise CLIConnectionError(str(event.get("message") or "OpenTerminal runtime failed to start"))
                    raise CLIConnectionError("OpenTerminal runtime did not acknowledge startup")
        except BaseException:
            await self.close()
            raise

    async def _receive(self) -> None:
        try:
            while True:
                event = await self._frame()
                kind = event["type"]
                if kind == "stdout":
                    data = event.get("data")
                    if not isinstance(data, dict):
                        raise CLIConnectionError("OpenTerminal stdout must be a parsed SDK message")
                    await self._messages.put(data)
                elif kind == "stderr":
                    await self._emit_stderr(event.get("data"))
                elif kind == "exit":
                    code = event.get("code")
                    if not isinstance(code, int) or isinstance(code, bool):
                        raise CLIConnectionError("Invalid OpenTerminal runtime exit code")
                    self._exit_code = code
                    if code:
                        raise ProcessError("Claude process in OpenTerminal exited unsuccessfully", exit_code=code)
                    break
                elif kind == "error":
                    raise CLIConnectionError(str(event.get("message") or "OpenTerminal runtime error"))
                else:
                    raise CLIConnectionError(f"Unexpected OpenTerminal runtime envelope: {kind}")
        except asyncio.CancelledError:
            return
        except Exception as exc:
            # close() marks local shutdown before closing the WebSocket. Its
            # close frame may reach this reader before cleanup cancels it.
            if not self._closed:
                await self._messages.put(exc)
        finally:
            self._ready = False
            if not self._closed:
                await self._messages.put(_END)

    async def write(self, data: str) -> None:
        if not isinstance(data, str):
            raise TypeError("SDK transport input must be a raw NDJSON string")
        async with self._write_lock:
            if not self.is_ready() or self._input_ended:
                raise CLIConnectionError("OpenTerminal transport input is not writable")
            assert self._ws is not None
            await self._ws.send_json({"type": "stdin", "data": data})

    async def end_input(self) -> None:
        async with self._write_lock:
            if self._input_ended or self._closed:
                return
            if not self.is_ready():
                raise CLIConnectionError("OpenTerminal transport is not connected")
            assert self._ws is not None
            await self._ws.send_json({"type": "end_input"})
            self._input_ended = True

    async def read_messages(self) -> AsyncIterator[dict[str, Any]]:
        if self._read_started:
            raise CLIConnectionError("SDK messages already have a reader")
        self._read_started = True
        while True:
            item = await self._messages.get()
            if item is _END:
                return
            if isinstance(item, Exception):
                raise item
            yield item

    async def cancel(self) -> None:
        """Stop the remote child process and release this transport."""
        await self.close()

    async def _cleanup(self) -> None:
        try:
            async with asyncio.timeout(self._cleanup_timeout):
                if self._ws is not None and not self._ws.closed:
                    try:
                        await self._ws.send_json({"type": "close"})
                    finally:
                        await self._ws.close()
        except (Exception, asyncio.CancelledError):
            pass
        finally:
            if self._reader is not None:
                self._reader.cancel()
                try:
                    await self._reader
                except asyncio.CancelledError:
                    pass
            if self._owns_session and self._session is not None:
                await self._session.close()
            # Wake an SDK reader even if a close interrupted a silent process.
            if self._messages.full():
                self._messages.get_nowait()
            self._messages.put_nowait(_END)

    async def close(self) -> None:
        self._ready = False
        self._closed = True
        if self._cleanup_task is None:
            self._cleanup_task = asyncio.create_task(self._cleanup())
        try:
            await asyncio.shield(self._cleanup_task)
        except asyncio.CancelledError:
            # Raw asyncio cancellation can reach an SDK shielded close. Finish
            # our bounded cleanup before returning the cancellation to caller.
            await asyncio.shield(self._cleanup_task)
            raise
