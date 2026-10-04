import asyncio
import json
import unittest

from supervisor.task_plan import TaskPlanRunner, dependency_context, validate_plan


def entry(function, properties=None, required=None):
    return {"callable": function, "supervisor_governed": True,
            "spec": {"parameters": {"type": "object", "properties": properties or {},
                                     "required": required or []}}}


def task(identifier, tool, dependencies=None, arguments=None):
    return {"id": identifier, "title": identifier, "tool": tool,
            "arguments": arguments or {}, "depends_on": dependencies or []}


class TaskPlanTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_overlap_and_dependency_waits_for_both_results(self):
        both_running = asyncio.Event()
        entered = set()
        returned = set()
        captured = {}

        async def weather(location):
            entered.add(location)
            if len(entered) == 2:
                both_running.set()
            # A sequential implementation would deadlock at the first call.
            await asyncio.wait_for(both_running.wait(), 1)
            returned.add(location)
            return {"location": location, "source": "actual-test-tool"}

        async def code(context=""):
            self.assertEqual(returned, {"Beijing", "Shanghai"})
            captured["context"] = context
            return {"status": "completed", "verification": {"passed": True},
                    "artifacts": [{"path": "result.svg"}]}

        tools = {
            "get_current_weather": entry(weather, {"location": {"type": "string"}}, ["location"]),
            "delegate_coding_task": entry(code, {"context": {"type": "string"}}),
        }
        result = await TaskPlanRunner(tools).run([
            task("weather-a", "get_current_weather", arguments={"location": "Beijing"}),
            task("weather-b", "get_current_weather", arguments={"location": "Shanghai"}),
            task("code", "delegate_coding_task", ["weather-a", "weather-b"], {"context": "原始用户上下文"}),
        ])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["peak_parallel"], 2)
        first, second, dependent = result["tasks"]
        self.assertLess(max(first["started_at_monotonic"], second["started_at_monotonic"]),
                        min(first["ended_at_monotonic"], second["ended_at_monotonic"]))
        self.assertGreaterEqual(dependent["started_at_monotonic"],
                                max(first["ended_at_monotonic"], second["ended_at_monotonic"]))
        self.assertIn("原始用户上下文", captured["context"])
        self.assertIn("actual-test-tool", captured["context"])
        self.assertEqual(dependent["result"]["artifacts"], [{"path": "result.svg"}])
        self.assertTrue(any(event["task_id"] == "code" and event["status"] == "waiting"
                            for event in result["timeline"]))

    async def test_insufficient_retrieval_blocks_code_and_transitive_children(self):
        called = []

        async def retrieval():
            called.append("retrieval")
            return {"status": "insufficient_evidence", "missing_information": ["没有公式"]}

        async def forbidden_code():
            self.fail("Insufficient evidence must never start dependent coding")

        async def weather():
            called.append("weather")
            return {"temperature_c": 20}

        tools = {"delegate_retrieval_task": entry(retrieval), "delegate_coding_task": entry(forbidden_code),
                 "get_current_weather": entry(weather)}
        result = await TaskPlanRunner(tools).run([
            task("read", "delegate_retrieval_task"),
            task("code", "delegate_coding_task", ["read"]),
            task("after-code", "get_current_weather", ["code"]),
            task("independent", "get_current_weather"),
        ])
        self.assertEqual(result["status"], "partial_failed")
        self.assertEqual([item["status"] for item in result["tasks"]], ["failed", "blocked", "blocked", "succeeded"])
        self.assertEqual(set(called), {"retrieval", "weather"})
        self.assertIsNone(result["tasks"][1]["started_at_monotonic"])
        self.assertEqual(result["tasks"][0]["result"]["missing_information"], ["没有公式"])

    async def test_coding_without_independent_verification_cannot_release_dependents(self):
        async def code():
            return {"status": "completed", "verification": {"passed": False}}

        async def next_step():
            self.fail("The SDK completion alone does not satisfy dependency success")

        result = await TaskPlanRunner({"delegate_coding_task": entry(code),
                                       "get_current_weather": entry(next_step)}).run([
            task("code", "delegate_coding_task"), task("after", "get_current_weather", ["code"])])
        self.assertEqual([item["status"] for item in result["tasks"]], ["failed", "blocked"])

    async def test_entire_invalid_dag_rejected_before_execution(self):
        called = []

        async def weather():
            called.append(True)
            return {}

        tools = {"get_current_weather": entry(weather)}
        invalid_plans = [
            [task("ready", "get_current_weather"), task("x", "get_current_weather", ["missing"])],
            [task("ready", "get_current_weather"), task("x", "get_current_weather", ["y"]), task("y", "get_current_weather", ["x"])],
            [task("same", "get_current_weather"), task("same", "get_current_weather")],
            [task("x", "get_current_weather", ["x"])],
            [task("x", "get_current_weather", arguments={"__user__": {"id": "other"}})],
            [task("x", "get_current_weather", arguments={"unknown": True})],
        ]
        for plan in invalid_plans:
            with self.subTest(plan=plan):
                result = await TaskPlanRunner(tools).run(plan)
                self.assertEqual(result["status"], "invalid_arguments")
                self.assertFalse(result["execution_started"])
        self.assertEqual(called, [])

    async def test_permission_allowlist_and_suffix_boundaries(self):
        called = []

        async def execute():
            called.append(True)
            return {}

        tools = {
            "server_weather_get_current_weather": entry(execute),
            "create_jira_ticket": entry(execute),
            "execute_task_plan": entry(execute),
            "evilget_current_weather": entry(execute),
        }
        for name in ("create_jira_ticket", "execute_task_plan", "evilget_current_weather"):
            result = await TaskPlanRunner(tools).run([task("x", name)])
            self.assertEqual(result["status"], "invalid_arguments")
        result = await TaskPlanRunner(tools).run([task("x", "server_weather_get_current_weather")])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(called, [True])

    async def test_duplicate_specialist_delegation_cannot_bypass_budget(self):
        async def execute():
            self.fail("Invalid plans must not call tools")

        for name in ("delegate_retrieval_task", "delegate_coding_task"):
            result = await TaskPlanRunner({name: entry(execute)}).run([task("a", name), task("b", name)])
            self.assertEqual(result["status"], "invalid_arguments")

    async def test_real_parallel_cap_with_six_independent_tasks(self):
        first_wave = asyncio.Event()
        running = 0
        peak = 0
        calls = 0

        async def weather():
            nonlocal running, peak, calls
            running += 1
            calls += 1
            peak = max(peak, running)
            if running == 3:
                first_wave.set()
            await asyncio.wait_for(first_wave.wait(), 1)
            await asyncio.sleep(0)
            running -= 1
            return {"actual": calls}

        result = await TaskPlanRunner({"get_current_weather": entry(weather)}, max_parallel=99).run([
            task(f"t{index}", "get_current_weather") for index in range(6)])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["max_parallel"], 3)
        self.assertEqual(result["peak_parallel"], 3)
        self.assertEqual(peak, 3)
        self.assertEqual(calls, 6)

    async def test_exception_does_not_cancel_independent_success(self):
        async def bad():
            raise OSError("upstream unavailable")

        async def good():
            return {"source": "actual independent result"}

        result = await TaskPlanRunner({"get_current_weather": entry(bad),
                                       "get_weather_forecast": entry(good)}).run([
            task("bad", "get_current_weather"), task("good", "get_weather_forecast")])
        self.assertEqual([item["status"] for item in result["tasks"]], ["failed", "succeeded"])
        self.assertEqual(result["tasks"][0]["result"]["exception_type"], "OSError")
        self.assertEqual(result["tasks"][1]["result"]["source"], "actual independent result")

    async def test_cancellation_awaits_all_running_workers(self):
        both_running = asyncio.Event()
        never_release = asyncio.Event()
        active = set()
        cleaned = set()

        async def weather(location):
            active.add(location)
            if len(active) == 2:
                both_running.set()
            try:
                await never_release.wait()
            finally:
                active.remove(location)
                cleaned.add(location)

        runner = TaskPlanRunner({"get_current_weather": entry(weather, {"location": {"type": "string"}}, ["location"])})
        operation = asyncio.create_task(runner.run([
            task("a", "get_current_weather", arguments={"location": "a"}),
            task("b", "get_current_weather", arguments={"location": "b"}),
            task("waiting", "get_current_weather", ["a"], {"location": "c"}),
        ]))
        await asyncio.wait_for(both_running.wait(), 1)
        operation.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await operation
        self.assertEqual(active, set())
        self.assertEqual(cleaned, {"a", "b"})
        self.assertEqual(runner.active, 0)
        self.assertEqual([record["status"] for record in runner.records.values()], ["cancelled"] * 3)
        self.assertFalse(any(worker.get_name().startswith("task-plan:" + runner.plan_id)
                             for worker in asyncio.all_tasks() if not worker.done()))

    async def test_deadline_cancels_tool_and_keeps_actual_timing(self):
        cleaned = asyncio.Event()

        async def slow():
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()

        runner = TaskPlanRunner({"get_current_weather": entry(slow)}, timeout_seconds=1)
        result = await runner.run([task("slow", "get_current_weather"), task("after", "get_current_weather", ["slow"])])
        self.assertEqual(result["status"], "timed_out")
        self.assertTrue(cleaned.is_set())
        self.assertEqual([record["status"] for record in result["tasks"]], ["timed_out", "blocked"])
        self.assertIsNotNone(result["tasks"][0]["wall_seconds"])
        self.assertIsNone(result["tasks"][1]["started_at_monotonic"])

    async def test_native_status_snapshot_contains_real_running_and_waiting_states(self):
        events = []

        async def emit(event):
            events.append(event)

        async def retrieval():
            return {"status": "sufficient", "evidence": [{"chunk_id": "real-id", "quote": "原文依据"}]}

        async def code(context=""):
            self.assertIn("real-id", context)
            self.assertIn("原文依据", context)
            return {"status": "completed", "verification": {"passed": True}}

        result = await TaskPlanRunner({"delegate_retrieval_task": entry(retrieval),
                                       "delegate_coding_task": entry(code, {"context": {"type": "string"}})}, emitter=emit).run([
            task("read", "delegate_retrieval_task"), task("code", "delegate_coding_task", ["read"])])
        self.assertEqual(result["status"], "completed")
        snapshots = [event["data"] for event in events]
        self.assertTrue(any("read：执行中" in event["description"] and "code：等待依赖" in event["description"]
                            for event in snapshots))
        self.assertTrue(snapshots[-1]["done"])
        self.assertTrue(all(event["type"] == "status" for event in events))

    def test_bounded_dependency_context_retains_actual_citation_and_marks_truncation(self):
        report = {"status": "sufficient", "summary": "真实依据", "tool_evidence": ["x" * 30000],
                  "evidence": [{"chunk_id": "actual:1", "quote": "实际原文" * 1000, "filename": "真实资料.md"}]}
        context = dependency_context([{"id": "read", "tool": "delegate_retrieval_task", "result": report}])
        self.assertLess(len(context), 7000)
        self.assertIn("actual:1", context)
        self.assertIn("真实资料.md", context)
        self.assertIn('"quote_truncated": true', context)
        self.assertEqual(len(report["evidence"][0]["quote"]), 4000)


if __name__ == "__main__":
    unittest.main()
