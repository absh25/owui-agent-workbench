> Archived standalone Pipe documentation. The current collaboration architecture and installation are documented in the [root README](../README.md). This document preserves the earlier Pipe description; historical validation counts and defaults are not publication metrics.

# OpenWebUI Claude Code Pipe

Run [Claude Code](https://docs.claude.com/en/docs/claude-code/overview)'s agent loop from inside [Open WebUI](https://github.com/open-webui/open-webui) chats, via the [Claude Agent SDK](https://github.com/anthropics/claude-agent-sdk-python).

The local lab includes **协作助手**, a supervisor that retains OWUI's
native business tool loop and delegates specialist work to two agents:
`delegate_coding_task` uses Claude Agent SDK for code changes and tests;
`delegate_retrieval_task` runs its own retrieval, evidence review, and retry loop.
See [setup and live tests](../lab/README.md#supervisor--specialist-agents)
and [the implementation design](harness-design.md).
The latest [collaboration and editable Jira confirmation update](collaboration-update.md)
describes cleanup, deployment and response optimizations.

The lab includes a lightweight **ragflow** knowledge base for spectrum-sensing
documents: local uploads and SQLite storage, real Alibaba Bailian embeddings,
and a read-only retrieval MCP used by the retrieval agent. The main assistant
receives reviewed evidence and source citations through task delegation. Open
http://127.0.0.1:18004 and see [usage and validation](retrieval-lab.md)
and [the retrieval agent design](retrieval-agent-design.md).

Refresh OWUI and select **协作助手** (`owui-main`) for new chats.
This is the only lab Pipe entry. Registration preserves its tools and parameters,
removes the two known legacy Pipes, and moves old `owui_supervisor` model metadata
to `owui-main` without changing message text. Old Claude chat records are retained.
Knowledge-base document IDs such as `SS-TEST-042` belong to the stated source;
their appearance alone does not make them Jira issue keys.

The backend regression covers 138 tests, with six additional frontend-patch tests.
The live smoke reads
the entry's full tool configuration from `/api/models`, including business tools.
The collaboration entry routes knowledge-base questions to retrieval without calling Jira
and completes retrieval → coding with an independent
OpenTerminal test run. Insufficient evidence stops the current handoff before coding.

This is an Open WebUI **Pipe** that exposes Claude Code as a selectable model. Each chat gets its own isolated workspace directory; agent turns within the same chat resume the same Claude Code session, so context (files, prior tool calls) carries forward.

The recommended `owui_only` mode replaces only the planning/controller layer.
Open WebUI still owns tool selection, user access control, credentials, and tool
execution. On every request, the Pipe adapts that request's authorized
`__tools__` into an in-process MCP server for the Claude Agent SDK. Tools not
selected for the Workspace Model or not granted to the current user are never
shown to the agent.

## Features

- **Open WebUI tool bridge** — Jira, OpenTerminal, builtin, local, OpenAPI, and MCP tools remain managed by Open WebUI
- **Strict default isolation** — `owui_only` hides every Claude Code builtin; the agent sees only the current request's authorized OWUI tools
- **Agentic retry loop** — the model observes tool results/errors and may reformulate parameters before another call
- **Per-chat workspaces** — each `chat_id` gets a sandboxed working directory that persists across turns
- **Dual auth** — bring your own Anthropic **API key** (pay-per-token) *or* a **Claude Pro/Max OAuth token** (bills against your subscription)
- **Streaming UI** — tool calls render inline with previews; generated images/PDFs/CSVs surface as artifacts in the chat
- **Configurable valves** — model, permission mode, tool allowlist, max turns, workspace root, setting sources (`CLAUDE.md`)

## Requirements

- Open WebUI (any recent version with the Pipes/Functions framework)
- Python deps (auto-installed by Open WebUI from the file header):
  - `claude-agent-sdk>=0.1.60`
  - `anthropic>=0.40.0`
- The `claude` CLI must be available on the host running Open WebUI's Python backend (the SDK shells out to it). Install via `npm install -g @anthropic-ai/claude-code`.

## Legacy standalone Pipe installation

The local collaboration lab uses [its registration workflow](../lab/README.md#supervisor--specialist-agents).
The standalone Pipe below is retained as source for separate installations; do not
install it in the collaboration lab, where only **协作助手** is enabled.

1. In Open WebUI, go to **Workspace → Functions → +** (or **Admin Panel → Functions**).
2. Paste the contents of [`claude_agent_pipe.py`](../claude_agent_pipe.py) into the editor.
3. Save and enable the function.
4. Open the function's **Valves** and configure auth (one of):
   - `ANTHROPIC_API_KEY` — standard pay-per-token billing
   - `CLAUDE_CODE_OAUTH_TOKEN` — generate on a machine with a browser via `claude setup-token`; bills against your Pro/Max/Team subscription
5. A new model named **Claude Code** will appear in the model picker.

## Configuration (Valves)

| Valve | Default | Description |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | *(env)* | Anthropic API key. Falls back to the backend's env var. |
| `CLAUDE_CODE_OAUTH_TOKEN` | *(empty)* | Claude subscription OAuth token. Takes priority over the API key when set. |
| `MODEL` | `ANTHROPIC_MODEL` or `deepseek-ai/DeepSeek-V4-Flash` | Model ID accepted by the configured Anthropic-compatible endpoint. |
| `PERMISSION_MODE` | `default` | `default`, `acceptEdits`, `bypassPermissions`, `plan`, or `dontAsk`. |
| `ALLOWED_TOOLS` | `Read,Write,Edit,Bash,Glob,Grep,WebSearch,WebFetch` | Claude builtins used only in `hybrid` or `claude_only` mode. |
| `TOOL_MODE` | `owui_only` | `owui_only` exposes only OWUI tools; `hybrid` adds listed Claude builtins; `claude_only` preserves the legacy behavior. |
| `USE_FULL_CLAUDE_CODE_PROMPT` | `false` | Restore Claude Code's large coding persona. Keep disabled for lower latency with third-party models. |
| `COMPACT_AGENT_INSTRUCTION` | compact OWUI agent contract | Small execution/verification prompt used by the SDK when the full Claude Code prompt is disabled. |
| `WORKDIR_ROOT` | `/app/backend/data/claude-agent-workspaces` | Root directory for per-chat workspaces. |
| `MAX_TURNS` | `30` | Max agent turns per user message. `0` disables the cap. |
| `MODEL_SILENCE_TIMEOUT_SECONDS` | `90` | Stop a turn after this many seconds without a new model/tool event; completed tools are never retried automatically. |
| `MODEL_INITIAL_RESPONSE_TIMEOUT_SECONDS` | `45` | Bound model waiting before the first tool call even if text events keep arriving. |
| `TOOL_FOLLOWUP_TIMEOUT_SECONDS` | `30` | Bound the model reply after the latest tool result, even while text events keep arriving. A subsequent tool call clears the deadline. |
| `FAST_READ_ONLY_TOOL_SUFFIXES` | `get_current_weather,get_weather_forecast` | Explicit read-only tools eligible for the shorter reply deadline. |
| `FAST_READ_ONLY_FOLLOWUP_TIMEOUT_SECONDS` | `15` | Model reply deadline after an eligible read-only tool succeeds. |
| `SETTING_SOURCES` | *(empty)* | Comma-separated filesystem setting sources to load: `user`, `project`, `local`. Empty = none (isolated baseline). See below. |

## Persistent context via `CLAUDE.md` (`SETTING_SOURCES`)

For the OWUI-owned long-term memory and SDK-owned short-term execution context,
see the [memory and runtime audit](memory-and-runtime-audit.md). Authorized
OWUI memory tools are included in the default builtin allowlist. SDK sessions are
scoped by user and chat. New sessions bootstrap recent OWUI history; resumed
sessions receive only conversation turns not already passed to the SDK. The
session index is currently process-local, so restarting the backend reconstructs
conversation context from OWUI rather than resuming the full prior SDK tool trace.

When the post-tool reply deadline expires, the Pipe clears the interrupted SDK
session and makes one text-only summary request using the already returned tool
results. That request has a 12-second timeout and receives no tools, so it cannot
repeat Jira writes or other actions. Intermediate model prose stays buffered:
third-party models can emit a tool-free planning message before a later tool call.
An empty final response is reported explicitly instead of appearing successful.
These deadlines bound model waiting, not the total duration of tool execution or
SDK process cleanup.

By default the pipe passes `setting_sources=[]` to the SDK, so **no** filesystem
settings are loaded: each chat starts from a clean baseline and does **not**
inherit the backend user's `~/.claude/` or the workdir's `.claude/`. This is the
safe default for shared deployments.

If you run a single-user/homelab instance and want persistent environmental
context (e.g. a host inventory or standing instructions in
`~/.claude/CLAUDE.md`) without re-explaining it every chat, set the valve:

| Value | Loads |
| --- | --- |
| *(empty)* | Nothing — isolated baseline (default). |
| `user` | `~/.claude/CLAUDE.md` **and** `~/.claude/settings.json`. |
| `user,project,local` | Above plus the workdir's `.claude/settings.json` and `.claude/settings.local.json`. |

Each token maps to one source — `user` → `~/.claude/`, `project` →
`<workdir>/.claude/settings.json`, `local` → `<workdir>/.claude/settings.local.json`.
Unknown tokens are dropped.

> [!WARNING]
> **Settings sources load more than `CLAUDE.md`.** A loaded `settings.json` can
> define **hooks that execute shell commands**, permission grants, env vars, and
> MCP servers — for *every chat*, under the backend user's identity, with the
> pipe's configured permission mode. Only enable `SETTING_SOURCES` on an
> instance you fully trust and control. **Do not enable it on multi-user or
> public deployments** — it breaks per-chat isolation and lets host config
> influence (or run code in) every user's session. There is no way to load
> `CLAUDE.md` *without* also loading `settings.json` from the same source; that
> coupling is in Claude Code, not this pipe.

### Does this apply to the sandboxed pipe?

Not directly. [`claude_agent_pipe_sandboxed.py`](../claude_agent_pipe_sandboxed.py)
doesn't use `setting_sources` at all — it shells the `claude` CLI inside an
open-terminal sandbox with a **per-chat `CLAUDE_CONFIG_DIR`** that's created
fresh each chat, so there's no host `~/.claude/` to inherit and nothing to
disable. Persistent context there is meant to come from mechanisms already built
for isolation:

- **Workspace Model system prompt** — appended on every turn (`--append-system-prompt`); the natural place for standing instructions.
- **Baking into the image** — skills are already vendored into the sandbox image at build time; a `CLAUDE.md` or settings can be baked the same way (under the per-chat `CLAUDE_CONFIG_DIR` layout) if you want file-based context.

Because the sandbox isolates the agent and a proxy holds the credentials, the
security tradeoff is far milder there — but the `setting_sources` valve itself
has nothing to act on, so it's intentionally **not** added to the sandboxed pipe.

## Auth notes

When both auth methods are present, the OAuth token wins and the API key is unset before invoking the SDK so it can't override.

## Tool ownership and execution

In `owui_only` mode the runtime path is:

```text
Open WebUI model/user tool selection
  -> request-scoped __tools__
  -> in-process MCP adapters
  -> Claude Agent SDK runs the configured model (DeepSeek by default)
  -> the model chooses a tool and the SDK manages the agent loop
  -> the bound Open WebUI callable executes
  -> result/error returns to the Agent loop
```

The Pipe never emits OpenAI `tool_calls` for Open WebUI to execute a second
time. It invokes the already-bound callable once inside the loop. Open WebUI
therefore remains the source of truth for tool credentials and permissions,
while the Claude Agent SDK owns planning, observation, retry, and final-answer
generation. Internal Open WebUI jobs such as title/tag generation receive no
business tools.

Plain greetings and requests without an authorized OWUI tool use the lighter
Messages API path. Both paths emit Open WebUI `status` events immediately and
at five-second intervals while waiting. SDK tool calls also emit a preparation
status, a call status with a collapsible input card, and a completion/failure
status. The SDK path buffers model prose until the final result because some
third-party models expose internal drafts in partial messages. If the model
stalls after a tool has run, the timeout message asks the user to verify the
tool result before retrying, avoiding duplicate writes such as Jira tickets.

For OpenTerminal, the MCP adapter supplies `wait=10` when `run_command` omits
it. Short commands therefore return their output inline without an extra model
round; commands still running after ten seconds retain the normal asynchronous
`process_id`/`get_process_status` flow. OWUI backend working-directory paths are
also removed before crossing into the isolated OpenTerminal container.

## Optional Tool Execution Middleware

The lab includes a request-scoped governance layer under `governance/`. When
`TOOL_GOVERNANCE_ENABLED=true`, every callable in the effective `__tools__`
map is wrapped before the SDK MCP adapter can execute it. The direct Messages
API is reserved for tool-free Open WebUI background jobs such as title/tag
generation:

```text
Claude tool_use -> ToolMiddleware -> PASS/AUTO/REVIEW/DENY -> callable/result
```

Tool selection remains owned by Open WebUI and Claude still sees the original
tool names and schemas. `policies/tool-governance.yaml` selects governed tools;
`policies/jira-workflow.yaml` contains the first deterministic Jira rules.
`REVIEW` writes an idempotent pending request to SQLite and never invokes the
original callable before approval. `DENY` is also terminal. PASS/AUTO results preserve the
original tool's return value. All paths write a redacted audit row.

The lab now defaults to requester confirmation in chat. With
`TOOL_GOVERNANCE_USER_CONFIRMATION=true`, governed Jira writes are previewed in
an Open WebUI confirmation dialog, and the authenticated requester must click
Confirm before the original callable executes. Cancel and invalid/disconnected
responses never authorize execution. The exact saved arguments are submitted
once per user/chat/message/tool/arguments combination, and model deadlines pause
while the dialog awaits a human. This mode does not create an approval Task in
Jira. The legacy group-admin approval flow described below requires disabling
this valve and supplying a workflow that routes review to group administrators.

Workflows may set `approver_group: current_user_group_admin` (or its alias
`requester_group_admin`). The middleware resolves this using the trusted
Open WebUI user context and records a target such as
`group-admins:wenguang`. The approval UI/service should use
`ApprovalStore.list_pending_for_groups(...)` and independently verify that the
reviewing account is an administrator of the group; membership alone is not
an approval grant.

The standalone pasted Function keeps governance disabled by default because
its companion Python package and policies are not embedded in the one-file
Function. The Docker lab mounts them at `/lab`, sets `PYTHONPATH=/lab`, and
enables governance. Configure these valves for another packaged deployment:

| Valve | Purpose |
| --- | --- |
| `TOOL_GOVERNANCE_ENABLED` | Enables the mandatory pre-call wrapper. |
| `TOOL_GOVERNANCE_POLICY` | Tool policy YAML path. |
| `TOOL_GOVERNANCE_JIRA_WORKFLOW` | Jira workflow YAML path. |
| `TOOL_GOVERNANCE_DATABASE` | SQLite approval/audit database path. |
| `TOOL_GOVERNANCE_APPROVAL_PROJECT` | Jira project key for generated approval Tasks (default `OWUI`). |
| `TOOL_GOVERNANCE_GROUP_ADMIN_IDS` | JSON map from Open WebUI group name to Jira admin `accountId`. |

For example, with the requester in group `wenguang`:

```dotenv
TOOL_GOVERNANCE_APPROVAL_PROJECT=OWUI
TOOL_GOVERNANCE_GROUP_ADMIN_IDS={"wenguang":"5b10..."}
```

When a governed operation needs review, the middleware creates a Jira Task
such as `[审批] company-jira_create_jira_ticket - REQ-...`, assigns it to the
configured administrator account, and returns its Jira key and URL in
`approval_issue`. The requested business operation remains unexecuted until a
separate approval executor verifies the Jira status/comment and performs it.

The legacy group-admin flow does not execute approved requests later: OWUI-bound
callables are request-scoped and cannot be serialized safely. A future Jira
approval executor must resolve an approved request through a separate Jira
Writer service rather than retaining the original Python callable.

Per Anthropic's terms: a Claude subscription is for personal use — **don't re-offer subscription auth to other end users** through a shared Open WebUI deployment. For multi-user setups, use API keys.

## License

MIT
