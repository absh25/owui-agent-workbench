from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock

from governance import ToolExecutionContext, ToolMiddleware, UserConfirmationGate
from governance.approval_store import arguments_hash, idempotency_key


class UserConfirmationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.database = Path(self.tempdir.name) / "approval.sqlite3"
        root = Path(__file__).parents[1]
        self.options = dict(
            policy_path=root / "policies/tool-governance.yaml",
            workflow_paths={"jira": root / "policies/jira-workflow.yaml"},
            database_path=self.database, require_user_confirmation=True,
        )
        self.context = ToolExecutionContext(user_id="user-1", chat_id="chat-1", message_id="message-1")
        self.arguments = {"project_key": "KAN", "summary": "测试提交", "description": "完整描述"}
        self.writer = AsyncMock(return_value={"key": "KAN-42"})

    def tearDown(self):
        self.tempdir.cleanup()

    async def execute(self, middleware, **overrides):
        kwargs = dict(tool_name="company-jira_create_jira_ticket", arguments=self.arguments,
                      original_callable=self.writer, context=self.context.for_tool("create_jira_ticket"))
        kwargs.update(overrides)
        return await middleware.execute(**kwargs)

    async def test_stops_before_write_and_submits_exact_preview_after_confirmation(self):
        entered = asyncio.Event()
        answer = asyncio.get_running_loop().create_future()
        events = []

        async def event_call(event):
            events.append(event)
            entered.set()
            return await answer

        gate = UserConfirmationGate(event_call)
        middleware = ToolMiddleware(**self.options, confirmation_callable=gate)
        task = asyncio.create_task(self.execute(middleware))
        await entered.wait()
        self.writer.assert_not_awaited()
        self.assertTrue(gate.is_waiting())
        self.assertIn("标题：测试提交", events[0]["data"]["message"])
        self.arguments["summary"] = "模型后来改动的内容"
        answer.set_result(True)
        self.assertEqual(await task, {"key": "KAN-42"})
        self.writer.assert_awaited_once_with(project_key="KAN", summary="测试提交", description="完整描述")
        self.assertFalse(gate.is_waiting())
        record = middleware.approvals.get_by_idempotency_key(
            idempotency_key(
                self.context, "company-jira_create_jira_ticket",
                {"project_key": "KAN", "summary": "测试提交", "description": "完整描述"},
            )
        )
        self.assertEqual(record["status"], "EXECUTED")
        self.assertEqual(record["approved_by"], "user-1")

    async def test_cancel_and_same_message_retry_do_not_submit_or_prompt_again(self):
        confirm = AsyncMock(return_value=False)
        middleware = ToolMiddleware(**self.options, confirmation_callable=confirm)
        first = await self.execute(middleware)
        second = await self.execute(middleware)
        self.assertEqual(first["status"], "CANCELLED")
        self.assertEqual(second["status"], "CANCELLED")
        changed = await self.execute(middleware, arguments={**self.arguments, "summary": "改参数重试"})
        self.assertEqual(changed["status"], "CANCELLED")
        confirm.assert_awaited_once()
        self.writer.assert_not_awaited()

    async def test_truthy_errors_strings_and_no_browser_never_approve(self):
        for answer in ({"error": "Client session disconnected."}, {"confirmed": True}, "yes", 1, None):
            with self.subTest(answer=answer):
                middleware = ToolMiddleware(**self.options, confirmation_callable=AsyncMock(return_value=answer))
                result = await self.execute(middleware)
                self.assertEqual(result["status"], "WAITING_FOR_APPROVAL")
                self.writer.assert_not_awaited()
        middleware = ToolMiddleware(**self.options, confirmation_callable=UserConfirmationGate(None))
        self.assertEqual((await self.execute(middleware))["status"], "WAITING_FOR_APPROVAL")
        self.writer.assert_not_awaited()

    async def test_missing_identity_never_submits(self):
        confirm = AsyncMock(return_value=True)
        middleware = ToolMiddleware(**self.options, confirmation_callable=confirm)
        result = await self.execute(middleware, context=replace(self.context, user_id=None))
        self.assertIn(result["status"], {"DENIED", "WAITING_FOR_APPROVAL"})
        confirm.assert_not_awaited()
        self.writer.assert_not_awaited()

    async def test_duplicate_confirmed_call_executes_once_and_new_message_can_submit_again(self):
        confirm = AsyncMock(return_value=True)
        middleware = ToolMiddleware(**self.options, confirmation_callable=confirm)
        self.assertEqual(await self.execute(middleware), {"key": "KAN-42"})
        self.assertEqual(await self.execute(middleware), {"key": "KAN-42"})
        self.writer.assert_awaited_once()
        confirm.assert_awaited_once()
        await self.execute(middleware, context=replace(self.context, message_id="message-2"))
        self.assertEqual(self.writer.await_count, 2)
        self.assertEqual(confirm.await_count, 2)

    async def test_concurrent_duplicate_calls_write_once(self):
        async def confirm(**kwargs):
            await asyncio.sleep(0.01)
            return True

        middleware = ToolMiddleware(**self.options, confirmation_callable=confirm)
        results = await asyncio.gather(self.execute(middleware), self.execute(middleware))
        self.assertEqual(results, [{"key": "KAN-42"}, {"key": "KAN-42"}])
        self.writer.assert_awaited_once()

    async def test_no_approval_ticket_is_created_before_user_confirmation(self):
        approval_writer = AsyncMock()
        middleware = ToolMiddleware(**self.options, confirmation_callable=AsyncMock(return_value=False),
                                    approval_issue_callable=approval_writer, approval_project_key="OWUI")
        await self.execute(middleware)
        approval_writer.assert_not_awaited()
        self.writer.assert_not_awaited()

    async def test_failure_after_confirmation_is_not_retried(self):
        emitter = AsyncMock()
        gate = UserConfirmationGate(AsyncMock(return_value=True), emitter)
        middleware = ToolMiddleware(**self.options, confirmation_callable=gate)
        self.writer.side_effect = RuntimeError("connection lost after submission")
        with self.assertRaises(RuntimeError):
            await self.execute(middleware)
        result = await self.execute(middleware)
        self.assertEqual(result["status"], "FAILED")
        self.writer.assert_awaited_once()
        self.assertFalse(gate.is_waiting())
        self.assertEqual(emitter.await_args_list[-1].args[0]["data"], {
            "description": "已确认提交内容。", "done": True,
        })
        self.assertEqual(emitter.await_count, 2)

    async def test_atomic_claim_requires_same_user_and_chat(self):
        middleware = ToolMiddleware(**self.options)
        result = await self.execute(middleware)
        for wrong_context in (replace(self.context, user_id="other"), replace(self.context, chat_id="other")):
            self.assertFalse(middleware.approvals.respond(result["request_id"], wrong_context, confirmed=True))
        self.assertTrue(middleware.approvals.respond(result["request_id"], self.context, confirmed=True))
        self.assertFalse(middleware.approvals.respond(result["request_id"], self.context, confirmed=True))

    async def test_form_edits_are_the_exact_persisted_and_executed_payload(self):
        seen = []

        async def event_call(event):
            form = event["data"]["jira_form"]
            seen.append(form)
            values = {field["name"]: field["value"] for field in form["fields"]}
            values.update(summary="用户修改的标题", description="最终描述\n第二行", priority="High")
            return {"confirmed": True, "request_id": form["request_id"], "arguments": values}

        middleware = ToolMiddleware(**self.options, confirmation_callable=UserConfirmationGate(event_call))
        returned = await self.execute(middleware)
        final = dict(project_key="KAN", summary="用户修改的标题", description="最终描述\n第二行",
                     issue_type="Task", priority="High", assignee_account_id=None)
        self.assertEqual(returned["submitted_arguments"], final)
        self.writer.assert_awaited_once_with(**final)
        record = middleware.approvals.get(seen[0]["request_id"])
        self.assertEqual(json.loads(record["arguments_json"]), final)
        self.assertEqual(record["arguments_hash"], arguments_hash(final))
        self.assertEqual(record["status"], "EXECUTED")
        # Retrying the original model call returns the existing result.
        await self.execute(middleware)
        self.writer.assert_awaited_once()
        self.assertEqual(len(seen), 1)

    async def test_invalid_edited_reply_cannot_authorize_write(self):
        for kind in ("stale", "unknown", "missing", "blank", "nonstring", "long"):
            with self.subTest(kind=kind):
                async def event_call(event):
                    form = event["data"]["jira_form"]
                    values = {field["name"]: field["value"] for field in form["fields"]}
                    request_id = form["request_id"]
                    if kind == "stale": request_id = "another-request"
                    elif kind == "unknown": values["approved"] = "true"
                    elif kind == "missing": values.pop("summary")
                    elif kind == "blank": values["summary"] = "  "
                    elif kind == "nonstring": values["summary"] = True
                    elif kind == "long": values["summary"] = "x" * 256
                    return {"confirmed": True, "request_id": request_id, "arguments": values}
                middleware = ToolMiddleware(**self.options, confirmation_callable=UserConfirmationGate(event_call))
                result = await self.execute(middleware)
                self.assertEqual(result["status"], "WAITING_FOR_APPROVAL")
                self.writer.assert_not_awaited()

    async def test_raw_structured_dict_is_not_a_trusted_confirmation(self):
        middleware = ToolMiddleware(**self.options, confirmation_callable=AsyncMock(return_value={
            "confirmed": True, "request_id": "REQ-any", "arguments": self.arguments,
        }))
        self.assertEqual((await self.execute(middleware))["status"], "WAITING_FOR_APPROVAL")
        self.writer.assert_not_awaited()

    async def test_type_choices_use_existing_authorized_read_result(self):
        events = []
        gate = UserConfirmationGate(AsyncMock(side_effect=lambda event: events.append(event) or False))
        middleware = ToolMiddleware(**self.options, confirmation_callable=gate)
        gate.observe_create_context([{"type": "text", "text": json.dumps({
            "status": "projects_available", "projects": [{"key": "KAN", "name": "我的软件团队"}]})}])
        reader = AsyncMock(return_value=(json.dumps({"status": "ready", "project": {"key": "KAN"},
            "issue_types": [{"id": "10003", "name": "任务", "untranslated_name": "Task"}]}), {}))
        await self.execute(middleware, tool_name="company-jira_get_jira_create_context",
                           arguments={"project_key": "KAN"}, original_callable=reader)
        await self.execute(middleware)
        field = next(field for field in events[0]["data"]["jira_form"]["fields"] if field["name"] == "issue_type")
        self.assertEqual(field["value"], "任务")
        self.assertEqual(field["options"], [{"value": "任务", "label": "任务"}])
        project_field = next(field for field in events[0]["data"]["jira_form"]["fields"] if field["name"] == "project_key")
        self.assertEqual(project_field["options"], [{"value": "KAN", "label": "KAN · 我的软件团队"}])
        reader.assert_awaited_once()
        self.writer.assert_not_awaited()

    async def test_edited_other_write_operations_keep_only_their_allowed_fields(self):
        for suffix, original, change in (
            ("update_jira_ticket", {"issue_key": "KAN-3", "summary": "旧标题"}, {"summary": "新标题"}),
            ("transition_jira_ticket", {"issue_key": "KAN-3", "target_status": "Done"}, {"target_status": "In Progress"}),
            ("add_jira_comment", {"issue_key": "KAN-3", "comment": "旧评论"}, {"comment": "新评论"}),
        ):
            async def event_call(event):
                form = event["data"]["jira_form"]
                values = {field["name"]: field["value"] for field in form["fields"]}
                values.update(change)
                return {"confirmed": True, "request_id": form["request_id"], "arguments": values}
            writer = AsyncMock(return_value={"key": "KAN-3"})
            middleware = ToolMiddleware(**self.options, confirmation_callable=UserConfirmationGate(event_call))
            await self.execute(middleware, tool_name="company-jira_" + suffix, arguments=original,
                               original_callable=writer)
            writer.assert_awaited_once_with(**{**original, **change})

    async def test_all_writes_require_confirmation_but_reads_and_delete_keep_policy(self):
        confirm = AsyncMock(return_value=False)
        middleware = ToolMiddleware(**self.options, confirmation_callable=confirm)
        for suffix, arguments in (
            ("create_jira_ticket", self.arguments),
            ("update_jira_ticket", {"issue_key": "KAN-3", "summary": "updated"}),
            ("transition_jira_ticket", {"issue_key": "KAN-3", "target_status": "Done"}),
            ("add_jira_comment", {"issue_key": "KAN-3", "comment": "test"}),
        ):
            middleware = ToolMiddleware(**self.options, confirmation_callable=confirm)
            result = await self.execute(middleware, tool_name="company-jira_" + suffix, arguments=arguments)
            self.assertEqual(result["status"], "CANCELLED")
        self.assertEqual(confirm.await_count, 4)
        self.writer.assert_not_awaited()
        result = await self.execute(middleware, tool_name="company-jira_delete_jira_ticket", arguments={"issue_key": "KAN-3"})
        self.assertEqual(result["status"], "DENIED")
        await self.execute(middleware, tool_name="company-jira_get_jira_ticket", arguments={"issue_key": "KAN-3"})
        self.writer.assert_awaited_once()
        self.assertEqual(confirm.await_count, 4)


class UserConfirmationGateStatusTests(unittest.IsolatedAsyncioTestCase):
    async def call_gate(self, gate):
        return await gate(request_id="request-1", tool_name="create_jira_ticket", arguments={
            "project_key": "KAN", "summary": "预览",
        })

    async def test_confirmation_completes_status_without_claiming_write_success(self):
        emitter = AsyncMock()
        gate = UserConfirmationGate(AsyncMock(return_value=True), emitter)
        self.assertIs(await self.call_gate(gate), True)
        self.assertEqual([call.args[0]["data"] for call in emitter.await_args_list], [
            {"description": "等待你确认提交内容……", "done": False},
            {"description": "已确认提交内容。", "done": True},
        ])
        self.assertFalse(gate.is_waiting())

    async def test_cancel_completes_status_and_preserves_false(self):
        emitter = AsyncMock()
        gate = UserConfirmationGate(AsyncMock(return_value=False), emitter)
        self.assertIs(await self.call_gate(gate), False)
        self.assertEqual(emitter.await_args_list[-1].args[0]["data"], {
            "description": "已取消提交。", "done": True,
        })
        self.assertFalse(gate.is_waiting())

    async def test_truthy_invalid_responses_do_not_get_confirmed_status(self):
        for response in ({"confirmed": True}, {"error": "disconnected"}, "yes", 1, None):
            with self.subTest(response=response):
                emitter = AsyncMock()
                gate = UserConfirmationGate(AsyncMock(return_value=response), emitter)
                self.assertIs(await self.call_gate(gate), response)
                self.assertEqual(emitter.await_args_list[-1].args[0]["data"], {
                    "description": "未取得有效确认，未提交。", "done": True,
                })
                self.assertFalse(gate.is_waiting())

    async def test_confirmation_exception_completes_status_and_propagates(self):
        emitter = AsyncMock()
        failure = RuntimeError("client disconnected")
        gate = UserConfirmationGate(AsyncMock(side_effect=failure), emitter)
        with self.assertRaises(RuntimeError) as raised:
            await self.call_gate(gate)
        self.assertIs(raised.exception, failure)
        self.assertEqual(emitter.await_args_list[-1].args[0]["data"], {
            "description": "未取得有效确认，未提交。", "done": True,
        })
        self.assertFalse(gate.is_waiting())

    async def test_emitter_exceptions_do_not_replace_confirmation_response(self):
        for response in (True, False, {"error": "client disconnected"}):
            with self.subTest(response=response):
                emitter = AsyncMock(side_effect=RuntimeError("status transport failed"))
                gate = UserConfirmationGate(AsyncMock(return_value=response), emitter)
                self.assertIs(await self.call_gate(gate), response)
                self.assertEqual(emitter.await_count, 2)
                self.assertFalse(gate.is_waiting())

    async def test_parallel_confirmation_statuses_finish_before_next_waiting(self):
        first_entered = asyncio.Event()
        complete_first = asyncio.Event()
        status_events = []
        call_count = 0

        async def event_call(event):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                first_entered.set()
                await complete_first.wait()
                return True
            return False

        async def emit(event):
            status_events.append(event["data"])
            await asyncio.sleep(0)

        gate = UserConfirmationGate(event_call, emit)
        first = asyncio.create_task(self.call_gate(gate))
        await first_entered.wait()
        second = asyncio.create_task(self.call_gate(gate))
        await asyncio.sleep(0)
        self.assertEqual(call_count, 1)
        self.assertTrue(gate.is_waiting())
        complete_first.set()
        self.assertEqual(await asyncio.gather(first, second), [True, False])
        self.assertEqual(status_events, [
            {"description": "等待你确认提交内容……", "done": False},
            {"description": "已确认提交内容。", "done": True},
            {"description": "等待你确认提交内容……", "done": False},
            {"description": "已取消提交。", "done": True},
        ])
        self.assertFalse(gate.is_waiting())


if __name__ == "__main__":
    unittest.main()
