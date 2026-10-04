from __future__ import annotations

import asyncio
import inspect
import json
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

from .approval_store import ApprovalStore
from .confirmation import ConfirmedArguments
from .audit import AuditLogger
from .models import ToolExecutionContext, WorkflowDecision
from .policy_loader import PolicyError, PolicyRegistry
from .workflow_engine import WorkflowEngine


_VALID_DECISIONS = {"PASS", "AUTO", "REVIEW", "DENY"}


class ToolMiddleware:
    """The single authorization boundary before an OWUI callable executes."""

    def __init__(
        self,
        *,
        policy_path: str | Path,
        workflow_paths: dict[str, str | Path],
        database_path: str | Path,
        approval_issue_callable: Callable[..., Any] | None = None,
        approval_project_key: str | None = None,
        group_admin_account_ids: dict[str, str] | None = None,
        require_user_confirmation: bool = False,
        confirmation_callable: Callable[..., Any] | None = None,
    ):
        self.policies = PolicyRegistry(policy_path)
        self.workflows = WorkflowEngine(workflow_paths)
        self.approvals = ApprovalStore(database_path)
        self.audit = AuditLogger(database_path)
        self.approval_issue_callable = approval_issue_callable
        self.approval_project_key = approval_project_key
        self.group_admin_account_ids = group_admin_account_ids or {}
        self.require_user_confirmation = require_user_confirmation
        self.confirmation_callable = confirmation_callable
        self._confirmation_lock = asyncio.Lock()
        self._confirmation_cancelled = False

    async def _confirm_and_execute(
        self, *, record: dict[str, Any], context: ToolExecutionContext,
        tool_name: str, original_callable: Callable[..., Any],
        decision: WorkflowDecision, policy_version: str | None,
    ) -> Any:
        # The exact persisted preview is also the exact payload submitted.
        arguments = json.loads(record["arguments_json"])
        pending = {
            "status": "WAITING_FOR_APPROVAL", "request_id": record["request_id"],
            "retry_allowed": False, "execution_performed": False,
            "instruction": "等待用户在确认弹窗中确认；不要重试或声称操作已经提交。",
        }
        if record["status"] == "EXECUTED":
            return json.loads(record["execution_result"])
        if record["status"] != "PENDING":
            return {**pending, "status": record["status"],
                    "instruction": "本请求已处理或执行结果尚未核实。不要重新提交。"}
        self.audit.write(
            context=context, tool_name=tool_name, arguments=arguments,
            policy_version=policy_version, decision=decision,
            status="WAITING_FOR_APPROVAL", result=pending,
        )
        if (not self.confirmation_callable or not context.user_id or not context.chat_id
                or record["user_id"] != context.user_id or record["chat_id"] != context.chat_id):
            return pending
        try:
            response = await self.confirmation_callable(
                request_id=record["request_id"], tool_name=tool_name,
                arguments=json.loads(record["arguments_json"]),
            )
        except Exception as exc:
            return {**pending, "confirmation_error": f"{type(exc).__name__}: {exc}"}
        edited = isinstance(response, ConfirmedArguments)
        if edited:
            if response.request_id != record["request_id"] or response.tool_name != tool_name:
                return {**pending, "confirmation_error": "确认请求不匹配，未提交。"}
            arguments = dict(response.arguments)
            # Edited payload must still satisfy the same user's workflow.
            policy = self.policies.policy_for(tool_name)
            if policy.get("mode") == "governed":
                check = self.workflows.evaluate(str(policy.get("workflow") or ""), tool_name, arguments, context)
                if check.decision == "DENY":
                    return {**pending, "status": "DENIED", "reason": check.reason}
        # Error dictionaries and strings are truthy but are never consent.
        if not edited and response is not True and response is not False:
            return {**pending, "confirmation_error":
                    str(response.get("error", "未收到有效确认")) if isinstance(response, dict)
                    else "未收到有效确认"}
        confirmed = edited or response is True
        if not self.approvals.respond(record["request_id"], context, confirmed=confirmed,
                                      arguments=arguments if edited else None):
            current = self.approvals.get(record["request_id"]) or record
            return {**pending, "status": current["status"], "duplicate": True}
        if response is False:
            self._confirmation_cancelled = True
            result = {**pending, "status": "CANCELLED",
                      "instruction": "用户取消了提交。告知用户已取消，不要再次发起此操作。"}
            self.audit.write(
                context=context, tool_name=tool_name, arguments=arguments,
                policy_version=policy_version, decision=decision, status="CANCELLED", result=result,
            )
            return result
        try:
            result = await self._call(original_callable, arguments)
        except BaseException as exc:
            status = "FAILED" if isinstance(exc, Exception) else "UNKNOWN"
            self.approvals.finish(record["request_id"], status=status,
                                  result={"error": f"{type(exc).__name__}: {exc}"})
            self.audit.write(
                context=context, tool_name=tool_name, arguments=arguments,
                policy_version=policy_version, decision=decision, status=status,
                error=f"{type(exc).__name__}: {exc}",
            )
            raise
        # OWUI HTTP headers are transport metadata, not part of the saved result.
        payload = result[0] if isinstance(result, tuple) and len(result) == 2 else result
        if edited:
            payload = ({**payload, "submitted_arguments": arguments} if isinstance(payload, dict)
                       else {"result": payload, "submitted_arguments": arguments})
            result = (payload, result[1]) if isinstance(result, tuple) and len(result) == 2 else payload
        self.approvals.finish(record["request_id"], status="EXECUTED", result=payload)
        self.audit.write(
            context=context, tool_name=tool_name, arguments=arguments,
            policy_version=policy_version, decision=decision, status="SUCCESS", result=payload,
        )
        return result

    async def _call(self, original_callable: Callable[..., Any], arguments: dict[str, Any]) -> Any:
        result = original_callable(**arguments)
        if inspect.isawaitable(result):
            result = await result
        return result

    async def _create_approval_issue(
        self,
        *,
        record: dict[str, Any],
        context: ToolExecutionContext,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any] | None:
        if not self.approval_issue_callable or not self.approval_project_key:
            return None
        target = str(record.get("approver_group") or "")
        group = target.removeprefix("group-admins:") if target.startswith("group-admins:") else ""
        assignee = self.group_admin_account_ids.get(group)
        summary = f"[审批] {tool_name} - {record['request_id']}"
        description = "\n".join(
            [
                "这是一个由 Open WebUI 自动创建的审批请求。",
                f"审批请求号: {record['request_id']}",
                f"申请人: {context.user_name or context.user_id or 'unknown'}",
                f"审批目标: {target or '未配置'}",
                f"原始工具: {tool_name}",
                "原始参数(JSON): " + json.dumps(arguments, ensure_ascii=False, sort_keys=True, default=str),
                "请由目标用户组管理员在 Jira 中审核。批准前，原始操作不会执行。",
            ]
        )
        issue = await self._call(
            self.approval_issue_callable,
            {
                "project_key": self.approval_project_key,
                "summary": summary,
                "description": description,
                "issue_type": "Task",
                **({"assignee_account_id": assignee} if assignee else {}),
            },
        )
        if not isinstance(issue, dict):
            raise RuntimeError("Jira approval tool returned a non-object result")
        self.approvals.attach_approval_issue(record["request_id"], issue)
        return issue

    async def execute(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        original_callable: Callable[..., Any],
        context: ToolExecutionContext,
    ) -> Any:
        policy_version = None
        try:
            policy = self.policies.policy_for(tool_name)
            policy_version = policy.get("version")
            mode = str(policy.get("mode", "passthrough")).lower()
            if mode == "passthrough":
                decision = WorkflowDecision(decision="PASS", matched_rule="policy:passthrough")
            elif mode == "deny":
                decision = WorkflowDecision(
                    decision="DENY",
                    reason=policy.get("reason", "Tool execution is denied by policy."),
                    matched_rule="policy:deny",
                )
            elif mode == "governed":
                workflow = str(policy.get("workflow") or "")
                decision = self.workflows.evaluate(
                    workflow, tool_name, arguments, context
                )
            else:
                raise PolicyError(f"Unsupported policy mode {mode!r} for {tool_name!r}")
            if decision.decision not in _VALID_DECISIONS:
                raise PolicyError(f"Unsupported workflow decision: {decision.decision!r}")
            if self.require_user_confirmation and mode == "governed" and decision.decision != "DENY":
                decision = replace(decision, decision="REVIEW",
                                   approver_group=f"user:{context.user_id}")
        except PolicyError as exc:
            decision = WorkflowDecision(
                decision="DENY",
                reason=f"Governance configuration error: {exc}",
                matched_rule="governance:configuration-error",
            )

        if decision.decision == "DENY":
            result = {
                "status": "DENIED",
                "retry_allowed": False,
                "execution_performed": False,
                "reason": decision.reason,
                "instruction": "Do not retry this tool call. Inform the user that policy denied it.",
            }
            self.audit.write(
                context=context, tool_name=tool_name, arguments=arguments,
                policy_version=policy_version, decision=decision, status="DENIED", result=result,
            )
            return result

        if decision.decision == "REVIEW":
            if self.require_user_confirmation and self._confirmation_cancelled:
                result = {
                    "status": "CANCELLED", "retry_allowed": False,
                    "execution_performed": False,
                    "instruction": "用户已取消本轮提交。停止后续写入，不要改变参数后再次提交。",
                }
                self.audit.write(
                    context=context, tool_name=tool_name, arguments=arguments,
                    policy_version=policy_version, decision=decision,
                    status="CANCELLED", result=result,
                )
                return result
            record, created = self.approvals.create_pending(
                context=context,
                tool_name=tool_name,
                arguments=arguments,
                decision=decision,
                policy_version=str(policy_version or "unknown"),
            )
            if self.require_user_confirmation:
                async with self._confirmation_lock:
                    return await self._confirm_and_execute(
                        record=self.approvals.get(record["request_id"]) or record,
                        context=context, tool_name=tool_name,
                        original_callable=original_callable, decision=decision,
                        policy_version=policy_version,
                    )
            approval_issue = None
            approval_issue_error = None
            if created or not record.get("approval_issue_key"):
                try:
                    approval_issue = await self._create_approval_issue(
                        record=record,
                        context=context,
                        tool_name=tool_name,
                        arguments=arguments,
                    )
                except Exception as exc:
                    approval_issue_error = f"{type(exc).__name__}: {exc}"
            elif record.get("approval_issue_key"):
                approval_issue = {
                    "key": record.get("approval_issue_key"),
                    "url": record.get("approval_issue_url"),
                }
            result = {
                "status": "WAITING_FOR_APPROVAL",
                "request_id": record["request_id"],
                "retry_allowed": False,
                "execution_performed": False,
                "duplicate": not created,
                "reason": record.get("reason"),
                "approver_group": record.get("approver_group"),
                "approval_issue": approval_issue,
                "approval_issue_error": approval_issue_error,
                "instruction": "Do not retry this tool call. Inform the user that approval is pending and the original tool was not executed.",
            }
            self.audit.write(
                context=context, tool_name=tool_name, arguments=arguments,
                policy_version=policy_version, decision=decision,
                status="WAITING_FOR_APPROVAL", result=result,
            )
            return result

        try:
            result = await self._call(original_callable, arguments)
        except Exception as exc:
            self.audit.write(
                context=context, tool_name=tool_name, arguments=arguments,
                policy_version=policy_version, decision=decision, status="FAILED",
                error=f"{type(exc).__name__}: {exc}",
            )
            raise
        if tool_name.endswith("get_jira_create_context"):
            observe = getattr(self.confirmation_callable, "observe_create_context", None)
            if callable(observe):
                observe(result)
        self.audit.write(
            context=context, tool_name=tool_name, arguments=arguments,
            policy_version=policy_version, decision=decision, status="SUCCESS", result=result,
        )
        return result
