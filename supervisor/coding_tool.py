"""
title: 编码任务
description: 将仓库修改和测试委派给 Claude Agent SDK，仅使用当前用户的 OpenTerminal。
version: 0.1.0
"""

import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import time
import uuid

from pydantic import BaseModel, Field
from supervisor.artifacts import deliver_html
from supervisor.pelican_demo import run_pelican_demo, validate_scene_request
from supervisor.verification import (
    AcceptanceVerifier, check_commands, elapsed_ms, parse_report,
    run_verified_session, snapshot, timed_phase,
)

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, ResultMessage
from claude_agent_pipe import (
    _build_owui_mcp_server, _sdk_tool_name_bridge, _CLAUDE_BUILTIN_TOOLS,
    _unwrap_owui_tool_result, _owui_tool_result_is_error,
)


_workspace_locks = {}
CODING_TOOLS = {"run_command", "get_process_status", "read_file", "write_file", "replace_in_file",
                "list_files", "search_files", "get_file_info", "apply_patch"}


@asynccontextmanager
async def workspace_lease(lock, wait_seconds, timings):
    """Bound queue time without cancelling the current holder or weakening isolation."""
    acquired = False
    waiting = time.monotonic()
    try:
        try:
            async with asyncio.timeout(wait_seconds):
                await lock.acquire()
                acquired = True
        except TimeoutError:
            pass
        finally:
            timings['lock_wait_ms'] = elapsed_ms(waiting)
        yield acquired
    finally:
        if acquired:
            lock.release()


def project_directory(repo, workspace):
    root = PurePosixPath(workspace)
    path = PurePosixPath(repo) if repo.startswith("/") else root / repo
    if ".." in path.parts or not path.is_relative_to(root):
        raise ValueError("编码目录必须位于当前用户的 OpenTerminal 工作区内。")
    return str(path)


def invalid_arguments(error, timings=None):
    """Argument correction has not consumed an SDK task or repair budget."""
    return {"status": "invalid_arguments", "stage": "validation", "execution_started": False,
            "retryable": True, "summary": str(error), "changed_files": [], "tool_evidence": [],
            "timings": timings or {},
            "correction": "仅修正委派参数后重试。纯 HTML/SVG 绘图使用 verification_commands=[]，"
                          "preview_target 为项目内相对文件名（例如 cat_doodle.svg），repo 默认为 .；"
                          "不要指定 /home/user 或用 test、ls、echo 代替浏览器验收。"}


def validate_arguments(verification_commands, preview_target, repo):
    if not isinstance(verification_commands, list):
        raise ValueError("verification_commands 必须是命令列表；纯 HTML/SVG 使用空列表。")
    check_commands(verification_commands, {"files": {}, "scripts": {}})
    if not isinstance(repo, str) or not repo or ".." in PurePosixPath(repo).parts:
        raise ValueError("repo 必须为当前工作区内的项目目录，默认使用 .。")
    if not isinstance(preview_target, str):
        raise ValueError("preview_target 必须是项目内文件名或本地应用 URL。")
    if preview_target and not preview_target.startswith("http://"):
        target = PurePosixPath(preview_target)
        if ".." in target.parts or target.suffix.lower() not in {".html", ".htm", ".svg"}:
            raise ValueError("preview_target 必须是项目内 HTML/SVG 文件或本地应用 HTTP URL。")
        if target.is_absolute() and not str(target).startswith("/workspace/"):
            raise ValueError("预览文件不能保存在 /home/user 等目录；使用项目内相对文件名。")


def structured_result(result, task_id, repo, evidence, verification=None, timings=None):
    """Never infer completion or test success from an unfinished SDK run."""
    if result is None:
        return {"status": "failed", "summary": "SDK 未返回完成结果。", "task_id": task_id,
                "repo": repo, "tool_evidence": evidence, "timings": timings or {}, "needs_approval": True}
    text = str(result.result or "")
    report = parse_report(text)
    written = [e["arguments"]["path"] for e in evidence
               if e["tool"] in {"write_file", "replace_in_file"} and not e["is_error"]
               and e["arguments"].get("path")]
    commands = [{"command": e["arguments"].get("command"),
                 "exit_code": e.get("exit_code"), "process_status": e.get("process_status"),
                 "output": e["result"], "is_error": e["is_error"]}
                for e in evidence if e["tool"] in {"run_command", "get_process_status"}]
    sdk_ok = not result.is_error and result.subtype == "success"
    verified = isinstance(verification, dict) and verification.get("passed") is True
    checks = (verification or {}).get("checks", [])
    return {"status": "failed" if not sdk_ok else "completed" if verified else "verification_failed",
            "summary": report.get("summary", text) if not sdk_ok or verified else "验收未通过，已有改动仍需检查和修复。",
            "changed_files": verification.get("changed_files", []) if verification is not None
                             else report.get("changed_files", sorted(set(written))),
            "tests": {"report": report.get("tests"), "command_results": commands,
                      "status": "passed" if checks and all(check.get("passed", check.get("exit_code") == 0) for check in checks)
                                else "failed" if checks else "not_run"},
            "verification": verification or {"passed": False, "status": "not_run",
                                               "issues": ["没有独立验收结果。"]},
            "open_issues": list((verification or {}).get("issues", [])) or report.get("open_issues", []),
            "task_id": task_id, "repo": repo,
            "tool_evidence": evidence, "timings": timings or {}, "needs_approval": True}


class Tools:
    class Valves(BaseModel):
        TERMINAL_ID: str = "local-open-terminal"
        MODEL: str = Field(default_factory=lambda: os.environ.get("ANTHROPIC_MODEL", ""))
        MAX_TURNS: int = 20
        TIMEOUT_SECONDS: int = 600
        WORKSPACE_WAIT_SECONDS: int = Field(default=30, ge=1, le=300)
        MAX_REPAIR_ATTEMPTS: int = Field(default=2, ge=0, le=3)
        VISUAL_REVIEW_MODEL: str = ""
        ENABLE_PELICAN_DEMO: bool = False
        DEMO_RETURN_SECONDS: int = Field(default=180, ge=1, le=300)

    def __init__(self):
        self.valves = self.Valves()

    async def delegate_coding_task(self, task: str, repo: str = ".", context: str = "",
                                   acceptance: str = "", verification_commands: list[str] = [],
                                   preview_target: str = "", read_only: bool = False, demo_scene: str = "",
                                   __request__=None, __user__: dict = None,
                                   __metadata__: dict = None, __event_emitter__=None) -> dict:
        """执行明确的编码任务并返回改动、测试和工具证据。仅编辑和验证，不提交、不 push、不发布。
        :param task: 需要完成的代码修改、仓库分析或测试任务。
        :param repo: 当前 OpenTerminal 工作区内的项目目录；默认当前目录。
        :param context: 完成该任务必需的业务上下文，不要传整段聊天。
        :param acceptance: 验收要求与应运行的测试。
        :param verification_commands: 真实测试/构建命令。纯 HTML/SVG 绘图必须传 []，系统会检查文件并运行浏览器截图验收，不要传 test/ls/echo。
        :param preview_target: 网页预览入口：项目内 HTML/SVG 文件，或 OpenTerminal 中本地应用的 HTTP URL。
        :param read_only: 仅仓库分析且不修改文件时设为 true；程序会核对是否实际修改。
        :param demo_scene: 默认空字符串，执行普通 SDK 编码。仅管理员开启预制演示且用户请求鹈鹕骑车时可传 pelican_bike；这会交付真实检查的预制作品，不调用 SDK。
        """
        started = time.monotonic()
        timings = dict.fromkeys(('preflight_ms', 'lock_wait_ms', 'initial_snapshot_ms',
                                 'sdk_startup_ms', 'sdk_and_verification_ms', 'delivery_ms', 'total_ms'), 0)
        timings.update({'tools': [], 'attempts': [], 'tool_total_ms': 0})
        verifier = None

        def finish(report):
            timings['attempts'] = list(getattr(verifier, 'attempt_timings', []))
            timings['tool_total_ms'] = round(sum(item['duration_ms'] for item in timings['tools']), 2)
            timings['total_ms'] = elapsed_ms(started)
            report['timings'] = timings
            return report

        from open_webui.models.users import Users
        from open_webui.utils.tools import get_terminal_tools

        try:
            validate_arguments(verification_commands, preview_target, repo)
            validate_scene_request(demo_scene, self.valves.ENABLE_PELICAN_DEMO, read_only,
                                   verification_commands, preview_target, context, acceptance)
        except ValueError as exc:
            timings['preflight_ms'] = elapsed_ms(started)
            return finish(invalid_arguments(exc))
        if not __request__ or not (__user__ or {}).get("id"):
            raise RuntimeError("编码委派需要经过 OWUI 认证的请求。")
        user = await Users.get_user_by_id(__user__["id"])
        if user is None:
            raise RuntimeError("当前用户不存在。")
        # OWUI performs its normal connection grants and context checks here.
        terminal_id = (__metadata__ or {}).get("terminal_id") or self.valves.TERMINAL_ID
        terminal_result = await get_terminal_tools(__request__, terminal_id, user,
            {"__user__": __user__, "__metadata__": __metadata__ or {}})
        terminal_tools = terminal_result[0] if isinstance(terminal_result, tuple) else terminal_result
        description = terminal_tools["run_command"]["spec"].get("description", "")
        match = re.search(r"current working directory is:\s*([^\r\n]+)", description, re.I)
        if not match:
            raise RuntimeError("无法确认 OpenTerminal 的工作目录。")
        try:
            directory = project_directory(repo, match.group(1).strip())
            if preview_target and not preview_target.startswith("http://"):
                project_directory(preview_target, directory)
        except ValueError as exc:
            timings['preflight_ms'] = elapsed_ms(started)
            return finish(invalid_arguments(exc))
        task_id, evidence = uuid.uuid4().hex, []

        async def emit(description, done=False):
            if __event_emitter__:
                try:
                    await __event_emitter__({"type": "status", "data": {
                        "description": description, "done": done}})
                except Exception:
                    # Losing a progress event does not authorize skipping or
                    # failing the actual coding and acceptance operations.
                    pass

        # A single-process lock is sufficient for this local, single-user lab.
        lock = _workspace_locks.setdefault(terminal_id, asyncio.Lock())
        timings['preflight_ms'] = elapsed_ms(started)
        if lock.locked():
            await emit(f"工作区正被另一项编码任务占用，最多排队 {self.valves.WORKSPACE_WAIT_SECONDS} 秒；当前尚未启动 SDK。")
        else:
            await emit("编码任务已委派，等待工作区…")
        async with workspace_lease(lock, self.valves.WORKSPACE_WAIT_SECONDS, timings) as acquired:
            if not acquired:
                summary = (f"工作区仍被另一项编码任务占用，排队已达 {self.valves.WORKSPACE_WAIT_SECONDS} 秒。"
                           "本任务尚未开始编码；请等当前任务结束后重试，或在页面中检查你自己的其他编码会话。")
                await emit(summary, True)
                return finish({'status': 'workspace_busy', 'execution_started': False,
                    'summary': summary, 'task_id': task_id, 'repo': directory,
                    'changed_files': [], 'tool_evidence': [], 'open_issues': [],
                    'verification': {'passed': False, 'status': 'not_run',
                                     'issues': [], 'checks': []}, 'needs_approval': True})
            await emit("工作区已就绪，正在检查目录与记录文件状态…")
            # Reject symlink escapes, on the actual terminal rather than the backend.
            check = ("import os; r=os.path.realpath(" + repr(match.group(1).strip()) + "); "
                     "p=os.path.realpath(" + repr(directory) + "); "
                     "assert os.path.commonpath([r,p])==r and os.path.isdir(p), 'invalid project directory'")
            with timed_phase(timings, 'preflight_ms'):
                checked = _unwrap_owui_tool_result(await terminal_tools["run_command"]["callable"](
                    command="python -c " + shlex.quote(check), wait="10"))
            if _owui_tool_result_is_error(checked, terminal=True):
                raise RuntimeError("编码项目目录不存在或不在当前工作区内。")
            with timed_phase(timings, 'initial_snapshot_ms'):
                initial = await snapshot(terminal_tools, directory, _unwrap_owui_tool_result)
            if not demo_scene:
                try:
                    verifier = AcceptanceVerifier(terminal_tools, directory, _unwrap_owui_tool_result,
                        initial, verification_commands, task, acceptance, preview_target, read_only,
                        self.valves.VISUAL_REVIEW_MODEL or self.valves.MODEL, emit=emit)
                except ValueError as exc:
                    await emit("委派参数需要修正；编码尚未开始。", True)
                    return finish(invalid_arguments(exc))

            tools = {}
            for name, entry in terminal_tools.items():
                if name not in CODING_TOOLS:
                    continue
                original = entry["callable"]

                def bind(tool_name, fn):
                    async def execute(**params):
                        if tool_name in {"write_file", "replace_in_file"} and params.get("path"):
                            params["path"] = project_directory(params["path"], directory)
                        if tool_name == "run_command":
                            params["cwd"] = directory
                            params.setdefault("wait", "10")
                        await emit("编码工具正在运行：" + tool_name)
                        tool_started = time.monotonic()
                        measured = {'tool': tool_name, 'duration_ms': 0, 'is_error': True}
                        try:
                            value = await fn(**params)
                            payload = _unwrap_owui_tool_result(value)
                            measured['is_error'] = _owui_tool_result_is_error(payload, terminal=True)
                        finally:
                            measured['duration_ms'] = elapsed_ms(tool_started)
                            timings['tools'].append(measured)
                        evidence.append({"tool": tool_name,
                            "arguments": {k: v for k, v in params.items() if k not in {"content", "old_text", "new_text", "patch"}},
                            "result": str(payload)[:5000],
                            "exit_code": payload.get("exit_code") if isinstance(payload, dict) else None,
                            "process_status": payload.get("status") if isinstance(payload, dict) else None,
                            "is_error": measured['is_error'], "duration_ms": measured['duration_ms']})
                        await emit("编码工具：" + tool_name)
                        return value
                    return execute
                tools[name] = {**entry, "callable": bind(name, original)}

            if demo_scene:
                try:
                    async with asyncio.timeout(self.valves.TIMEOUT_SECONDS):
                        report = await run_pelican_demo(task_id=task_id, directory=directory,
                            terminal_tools=terminal_tools, coding_tools=tools, user_id=user.id,
                            initial=initial, unwrap=_unwrap_owui_tool_result, emit=emit,
                            timings=timings, started=started, return_seconds=self.valves.DEMO_RETURN_SECONDS)
                except TimeoutError:
                    await emit('预制演示检查或交付超时，未完成。', True)
                    report = {'status': 'failed', 'execution_mode': 'demo_fixture', 'sdk_used': False,
                        'scene': demo_scene, 'summary': '预制演示检查或交付超时，未完成。',
                        'verification': {'passed': False, 'status': 'timed_out'},
                        'task_id': task_id, 'repo': directory, 'artifacts': []}
                report['tool_evidence'] = evidence
                return finish(report)

            server, names, _ = _build_owui_mcp_server(tools)
            runtime_key = hashlib.sha256((user.id + task_id).encode()).hexdigest()[:24]
            runtime_dir = Path("/app/backend/data/coding-agent-runtime") / runtime_key
            runtime_dir.mkdir(parents=True, exist_ok=True)
            options = {"cwd": str(runtime_dir), "model": self.valves.MODEL,
                "tools": [], "disallowed_tools": list(_CLAUDE_BUILTIN_TOOLS),
                "allowed_tools": names, "mcp_servers": {"openwebui": server},
                "strict_mcp_config": True, "setting_sources": [], "permission_mode": "default",
                "max_turns": self.valves.MAX_TURNS,
                "env": {"IS_SANDBOX": "1"},
                "system_prompt": f"""你是编码执行器，使用简体中文。实际项目目录：{directory}。
所有文件读写和命令都通过 mcp__openwebui__ 工具在 OpenTerminal 执行。
生成的 HTML、SVG 和其他交付文件必须保存在实际项目目录内。不要另写 /home/user 或 /tmp；
它们不在用户右侧文件栏的目录内。完成前读取实际生成文件，changed_files 返回真实路径。
运行时 cwd 是宿主运行目录，不是项目；不要使用宿主的文件和命令工具。
只完成传入的编码任务并运行适当测试。不执行 git commit、push、发布或任何 Jira 操作。
局部修复时先集中读取用户指定的目标文件与固定测试；上下文足够后直接做最小实现。
不要无目的扫描整个工作区、安装无关依赖、重构无关文件或反复重新读取没有变化的内容。
运行固定测试并核对实际退出码；反馈失败时针对真实问题修复，不重复成功步骤。
完成后系统将独立执行固定验收命令；失败、未执行或网页截图不合格不能交付。
不要删除测试、改弱断言、吞掉退出码或篡改验收服务。收到验收反馈时继续修复同一任务。
网页必须可在桌面和手机运行，动画的主体姿态与各部分运动关系必须成立。
需要本地开发服务器时在 OpenTerminal 启动并保持其运行，绑定 0.0.0.0 而非仅回环地址，提供实际可访问入口。
工具完整名称：{', '.join(names)}。短命令 wait=10，后台命令轮询状态。
完成后只返回 JSON 对象：summary、changed_files（路径列表）、tests（命令与真实结果）、
open_issues（未解决问题列表）。JSON 保持简短，不重复解释代码；没有运行的测试必须写明未运行。"""}
            prompt = json.dumps({"task": task, "repo": directory, "context": context,
                                 "acceptance": acceptance, "verification_commands": verifier.commands,
                                 "preview_target": preview_target, "read_only": read_only}, ensure_ascii=False)
            result, verification = None, None
            await emit("正在启动 Claude Agent SDK…")
            sdk_started = time.monotonic()
            sdk_ready = False
            try:
                async with asyncio.timeout(self.valves.TIMEOUT_SECONDS):
                    async with _sdk_tool_name_bridge(options, names), ClaudeSDKClient(
                        options=ClaudeAgentOptions(**options)) as client:
                        timings['sdk_startup_ms'] = elapsed_ms(sdk_started)
                        sdk_ready = True
                        await emit("Claude Agent SDK 已就绪，正在读取任务文件…")
                        result, verification = await run_verified_session(client, prompt, verifier,
                            ResultMessage, self.valves.MAX_REPAIR_ATTEMPTS, emit)
            except TimeoutError:
                await emit("编码任务超时，已停止。请查看已有改动。", True)
                return finish({"status": "failed", "summary": "编码执行超时，已有改动需要检查。",
                        "task_id": task_id, "repo": directory, "tool_evidence": evidence,
                        "verification": verifier.last_outcome or {"passed": False, "status": "timed_out"},
                        "needs_approval": True})
            except Exception as exc:
                await emit("编码或验收执行失败，任务未完成。", True)
                return finish({"status": "failed", "summary": "编码或验收执行失败：" + str(exc),
                        "task_id": task_id, "repo": directory, "tool_evidence": evidence,
                        "verification": verifier.last_outcome or {"passed": False, "status": "failed"},
                        "needs_approval": True})
            finally:
                timings['sdk_and_verification_ms'] = elapsed_ms(sdk_started)
                if not sdk_ready:
                    timings['sdk_startup_ms'] = timings['sdk_and_verification_ms']
            report = structured_result(result, task_id, directory, evidence, verification, timings)
            if report['status'] == 'completed':
                await emit("验收通过，正在保存截图与交付文件…")
            with timed_phase(timings, 'delivery_ms'):
                if report["status"] == "completed" and verifier.images:
                    from supervisor.artifacts import save_screenshot
                    report["verification"]["screenshots"] = [await save_screenshot(user.id, image) for image in verifier.images]
                report["artifacts"], report["delivery_errors"] = await deliver_html(
                    report, terminal_tools, user.id, directory, _unwrap_owui_tool_result)
            await emit("编码、验收与交付阶段已结束。", True)
            return finish(report)
