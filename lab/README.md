# Local Open WebUI + Claude Agent harness lab

This lab is isolated under the Docker Compose project name
`owui-claude-agent-lab`. It runs:

- Open WebUI at http://127.0.0.1:13000
- Claude Code inside the Open WebUI container
- A local weather MCP at http://127.0.0.1:18001/mcp
- A local Jira MCP at http://127.0.0.1:18003/mcp
- An isolated OpenTerminal sandbox at http://127.0.0.1:18002
- A lightweight ragflow knowledge platform at http://127.0.0.1:18004

The Open WebUI image uses the v0.11.4 `slim` variant to keep this disposable
lab small. Local document extraction and bundled embedding models are not
included; Functions/Pipes and remote-model chat are included.
The separate `retrieval-lab` service extracts knowledge documents and calls
Alibaba Bailian for embeddings; it does not load a local model.

The runtime uses the Anthropic Messages protocol. Configure a compatible endpoint
in `.env`; an OpenAI-only endpoint is not sufficient.

## Supervisor + specialist agents

Refresh OWUI and select **协作助手** (`owui-main`) for a new chat.
This is the only lab Pipe entry, with retrieval, coding and business tools. The main assistant plans and
coordinates tasks through OWUI's native server-side tool loop:

- `delegate_retrieval_task` runs a separate model/tool loop to search the
  knowledge base, inspect evidence, retry queries, and decide when to stop.
- `delegate_coding_task` uses Claude Agent SDK to manage code changes,
  debugging, and tests in OpenTerminal.
- Jira and other business tools stay with the main assistant. Jira writes
  retain the preview and human confirmation gate.

The main assistant does not receive the three underlying ragflow MCP tools.
The registration script backs up the database, verifies the replacement, and removes
only `claude_code` and `owui_supervisor` plus their old model entries. It migrates
old raw-supervisor chats' model metadata to `owui-main`, preserving their messages.
Old Claude chat records, all tools, MCP connections and provider models remain intact.
The saved Pipe and model share `owui-main`; the model has no separate base ID,
so there is no duplicate selector entry. Re-running registration does not restore old Pipes.

After updating the mounted implementation, sync it and refresh the cache:

```powershell
docker compose exec -T open-webui python /lab/supervisor/register.py
docker compose restart open-webui
```

Try asking `查询 KAN-3 的标题和状态` for business tools,
`委派检索 Agent 查询 SS-TEST-042 的采样率和阈值，核对原文并给出引用`
for retrieval delegation, or
`在当前工作区创建 calculator.py 的 add 函数，写两个 unittest 并运行测试，不提交`
for coding delegation. Coding projects live in OpenTerminal's workspace,
`/workspace/wenguang.wu`, rather than the Windows host folder.

Try the complete flow in **协作助手**:

```text
查 ragflow 中 SS-TEST-042 的采样率和阈值并附原文引用。证据足够后，
委派编码 Agent 在当前工作区新目录生成演示参数模块、标记演示数据，
写至少两个单元测试并运行，不提交。
```

The current live flow passed using all configured model tools, including Jira:
retrieval returned actual source evidence, coding used that evidence, and an
independent OpenTerminal run passed two tests.
The example stores demo parameters as constants to verify evidence handoff;
it does not implement a real spectrum-sensing algorithm.
The negative case also passed: insufficient evidence ended the current task
without calling the coding agent. For current regression results and dependency-separated commands, see
[the publication check record](../docs/publishing-checks.md). The smoke reads the complete model tool
configuration from `/api/models` and verifies the single collaboration entry.
The entry is checked for both insufficient-evidence stopping and the full
retrieval → coding handoff; removed legacy IDs must not appear in the model list.

Run the focused checks and live smoke tests:

```powershell
docker compose exec -T open-webui python -m unittest tests.test_supervisor tests.test_retrieval_agent tests.test_user_confirmation tests.test_supervisor_registration
docker compose exec -T open-webui python /lab/supervisor/smoke_native.py
docker compose exec -T open-webui python /lab/supervisor/smoke_test.py
docker compose exec -T open-webui python /lab/supervisor/smoke_agents.py
docker compose exec -T open-webui python /lab/supervisor/smoke_jira_confirmation.py
docker compose exec -T -e PYTHONPATH=/lab:/app/backend open-webui python /lab/supervisor/smoke_coding_debug.py
```

The supervisor and SDK coding runtime use the configured upstream model
(currently `qwen3.7-plus`); the SDK name does not select a Claude model. The retrieval
agent has its own model setting (currently `qwen3.7-flash`). Its loop runs inside the existing
OWUI backend and needs no new container or local model. This implementation
does not add a task queue, background job service or resumable distributed
state. See [the design](../docs/harness-design.md).

The coding debug smoke starts with an intentionally broken function and two fixed
tests in its own terminal directory. It requires real failing tests, an implementation
edit, then passing tests; it independently reruns the tests and verifies their bytes
were not changed. Its temporary chat is removed, and the example files are retained
for inspection. It does not request Git or Jira actions.

Jira's editable review form is bundled into the image. Rebuild the image when
updating `frontend/`, then refresh existing pages normally. The installer versions
the chat module and its importing entries together, so the browser loads the updated
form after a refresh. The form submits
only the final user-confirmed draft; cancel stops the write. The confirmation smoke
above always cancels and never approves a real Jira submission. See
[deployment and confirmation details](../docs/collaboration-update.md).

## Lightweight ragflow retrieval

Open http://127.0.0.1:18004, upload TXT/Markdown/DOCX/text PDF files, and select
**协作助手** in OWUI to ask questions about the indexed documents. The knowledge
base is named `ragflow`; this is a small local implementation rather than the
official RAGFlow stack. It uses real Bailian `text-embedding-v4` embeddings,
SQLite storage, and three read-only MCP tools with source citations.

The main assistant delegates questions to the retrieval agent. The specialist
owns search, evidence review, query retries, and stopping; it returns a
structured conclusion, actual source excerpts, and any missing information.
The main assistant then coordinates the next task, including passing the
evidence to the coding agent. This version uses vector search and original
document chunks; it does not include a knowledge graph.

Document IDs such as `SS-TEST-042` are resolved from the source named by the user.
An ID in a ragflow question is not routed to Jira merely because it resembles
an issue key; unavailable retrieval tools do not trigger a Jira fallback.

Jira writes retain human confirmation. The knowledge service used approximately
88 MiB in this round's retrieval checks and has a 384 MiB limit; this is a
small-sample measurement rather than the whole system's peak memory usage.

See [setup, limits and test commands](../docs/retrieval-lab.md).

## Configure and register the collaboration entry

The current installation workflow is described in the [root README](../README.md#运行方式).
Configure `.env` with a real Anthropic Messages-compatible endpoint and model ID,
API key, distinct random service secrets, and `OPEN_TERMINAL_OWNER_NAME`.

First build the OWUI image, because the retrieval image uses it as a base:

```bash
docker compose build open-webui
docker compose up -d --build --wait
```

Create the first OWUI admin account at http://127.0.0.1:13000. Its display name
must match `OPEN_TERMINAL_OWNER_NAME`. Then register the authorized terminal,
the collaboration Pipe and task tools, and the knowledge connection:

```bash
docker compose exec -T open-webui python /lab/register_open_terminal.py
docker compose exec -T open-webui python /lab/supervisor/register.py
docker compose exec -T open-webui python /lab/retrieval_lab/register.py
docker compose restart open-webui
```

Compose starts services but does not automatically register Pipes or tool connections.
The single active collaboration entry is `owui-main` (协作助手). Do not paste the
legacy standalone Pipe into this lab. Model IDs for main/retrieval/coding can be
edited independently in their Valves; already saved values override environment defaults.
The three current agents share backend provider credentials, without per-user BYOK.

Jira registration is optional after its actual credentials have been configured.
The knowledge upload UI and single terminal volume are local lab resources, not a
complete implementation of tenant-isolated storage or per-user container provisioning.

## Local weather MCP

The Compose stack includes `本地天气 MCP`, backed by Open-Meteo. It exposes:

- `get_current_weather`
- `get_weather_forecast`

No API key is required. The registration target inside Docker is
`http://weather-mcp:8000/mcp`. Select **本地天气 MCP** from the chat tool picker
or attach it to a Workspace Model; Open WebUI then passes its two functions to
the Agent.

The Pipe defaults to `permissionMode=default` and stores per-chat workspaces
inside the isolated Open WebUI Docker volume.

## Tool governance lab

The Compose service enables the Tool Execution Middleware and mounts the
repository's `governance/` and `policies/` directories read-only. Pending
approval requests and redacted audit logs are stored in:

```text
/app/backend/data/tool-governance.sqlite3
```

The policy passes unlisted tools through unchanged and denies
`delete_jira_ticket`. Every Jira create, update, transition, or comment pauses
before submission and displays its full arguments in an Open WebUI confirmation
dialog. Only clicking Confirm submits that exact payload; Cancel, missing browser
sessions, and invalid/error responses leave the operation unexecuted. The lab's
Pipe enables `TOOL_GOVERNANCE_USER_CONFIRMATION` by default. No Jira approval
ticket is created before the user's confirmation.

Confirmation waits suspend the Pipe's model deadlines. Approval and execution
records bind the authenticated user, chat, assistant message, tool, and arguments.
Repeated calls for the same message do not submit twice. If submission fails or
is interrupted after confirmation, its result must be checked before another
attempt. A new message is a new request and requires a new confirmation.

To test without submitting a real write, select **Company Jira MCP** with the
governed Claude Code model and ask it to create a Task in your actual project.
Check the preview, click **Cancel**, and verify that the response reports the
cancellation and that no Task was created. Read-only Jira queries do not open
this dialog. Keep the current chat open while confirming.

## Jira MCP

The `jira-mcp` service is registered in Open WebUI as **Company Jira MCP** and
connects to Jira Cloud REST API v3. Configure these values in `.env`:

```dotenv
JIRA_BASE_URL=https://your-company.atlassian.net
JIRA_EMAIL=service-account@example.com
JIRA_API_TOKEN=your-atlassian-api-token
```

It exposes read tools (`search_jira`, `get_jira_ticket`,
`get_jira_comments`, `get_jira_create_context`) and governed write tools (`create_jira_ticket`,
`update_jira_ticket`, `transition_jira_ticket`, `add_jira_comment`,
`delete_jira_ticket`). Select **Company Jira MCP** on the governed Claude Code
Workspace Model. Do not attach this write-capable MCP to an ungoverned model:
the enforcement boundary lives in the Claude Pipe's Tool Middleware.

With empty credentials the MCP still starts and publishes its schemas, but any
Jira API call returns a configuration error. This permits safe connection and
middleware testing before a real account is supplied.

Before preparing a create request, the main assistant queries
`get_jira_create_context()` to discover real project keys, then calls it with
the selected key to check creation permission and allowed issue types. It must
not invent `TEST` or `DEVS`. The current lab account sees project `KAN`
(我的软件团队), can create issues there, and its task type is `任务` / `Task`
(ID `10003`). These are live observations, not hardcoded defaults. The client
rechecks metadata before any create POST and resolves localized type names to
the actual type ID. Jira's [project API](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-projects/),
[permission API](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-permissions/),
and [issue metadata API](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issues/)
provide this read-only context.

The confirmation status now completes after Confirm, Cancel, or an invalid
response; confirmation does not claim the subsequent write succeeded.
The previous failed `TEST` request was confirmed by its requester and rejected
by Jira. Its historical waiting text was a display defect, not a pending write.

Read-only end-to-end validation:

```powershell
docker compose exec -T open-webui python /lab/supervisor/smoke_jira_preflight.py
```

Live MCP metadata and OWUI project/permission/type discovery passed without
creating any issue; 112 regression tests passed, including the metadata and
confirmation status cases.

## OpenTerminal sandbox

The stack registers **wenguang.wu 的 OpenTerminal** as a single-user Open
WebUI terminal connection. Its access grant is bound to the immutable Open
WebUI user ID belonging to `wenguang.wu`; other Open WebUI users do not receive
this connection. Its custom `open-terminal:slim` image includes common
development tools, Python/Node tooling, HTML/XML validators, LibreOffice
headless conversion, DOCX/PPTX/XLSX libraries, PDF/image utilities, and CJK
fonts. The image also includes SSH/rsync, network and process diagnostics,
SQLite, DuckDB, Graphviz, Ghostscript/qpdf, ImageMagick, TypeScript/tsx/pnpm,
formatters, linters, and common Python HTTP/data libraries. It uses a dedicated
empty workspace at `/workspace/wenguang.wu`, a 2 GB
memory limit, two CPUs, and no host-directory or Docker socket mounts. `HOME`,
the default command working directory, and the visual file-browser root all
point at that workspace, so the sidebar label is `wenguang.wu`. The owner has
full read/write/execute access inside the sandbox, which cannot directly
modify the Windows host.

After changing the image, rebuild and recreate only this service:

```powershell
docker compose build open-terminal
docker compose up -d --force-recreate open-terminal
```

Useful checks from the OpenTerminal shell include `soffice --version`,
`pandoc --version`, `python -c "import docx, openpyxl, bs4"`,
`node --version`, `tsx --version`, `pnpm --version`, and `prettier --version`.

Choose **wenguang.wu 的 OpenTerminal** from the terminal selector in a chat. Open
WebUI then exposes its file sidebar and passes the selected terminal tools to
the Agent. Files survive ordinary container restarts in
`open-terminal-wenguang-wu`;
running `remove-lab.ps1` removes that disposable volume too.

The Pipe's `LANGUAGE_INSTRUCTION` valve defaults to:

> 你必须始终使用简体中文与用户交流。所有面向用户的解释、状态说明、工具调用摘要、错误说明和最终回答都必须使用中文；代码、命令、路径、专有名词及工具返回的原始内容可保留原文。即使用户使用其他语言提问，也要用简体中文回答。

## Stop or remove

Stop while retaining data:

```powershell
docker compose stop
```

Delete containers, network, the isolated data volume, and the locally built
Open WebUI image:

```powershell
.\remove-lab.ps1
```

After that, delete the `openwebui-claude-code-lab` directory if it is no
longer needed. Shared pulled base images are deliberately preserved because
other Docker projects may use them.
