"""Exercise the retrieval Agent with independent model/tool fakes, never live services."""

import asyncio
import fnmatch
import json
import sys
from types import ModuleType, SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock, patch

from supervisor.retrieval_tool import (
    RETRIEVAL_TOOLS, RetrievalRunner, Tools, connect_authorized_knowledge, parse_assessment,
    extract_original_request,
)


FILENAME = "频谱感知实验.md"
TEXT = "实验 SS-TEST-042 的采样率为 2 MHz，能量检测阈值为 0.12。这是虚构演示，不包含通用阈值计算公式。"
QUOTE = "采样率为 2 MHz，能量检测阈值为 0.12"
HIT = {"chunk_id": "chunk-1", "document_id": "doc-1", "filename": FILENAME,
       "page": 3, "ordinal": 0, "text": TEXT, "score": 0.99}


def response(*blocks, stop_reason="end_turn"):
    return NS(content=list(blocks), stop_reason=stop_reason)


def tool_call(name, arguments, identifier="call-1"):
    return NS(type="tool_use", id=identifier, name=name, input=arguments)


def assessment(status="sufficient", evidence=None, missing=None, summary="演示实验采样率 2 MHz，阈值 0.12。"):
    if evidence is None:
        evidence = [{"chunk_id": "chunk-1", "quote": QUOTE}]
    return response(NS(type="text", text=json.dumps({
        "status": status, "summary": summary, "evidence": evidence,
        "missing_information": [] if missing is None else missing,
    }, ensure_ascii=False)))


def fake_client(responses=()):
    return NS(messages=NS(create=AsyncMock(side_effect=list(responses))))


def fake_tools(indexed=1):
    overview = {"document_count": 1, "indexed_chunk_count": indexed,
                "documents": [{"id": "doc-1", "filename": FILENAME,
                               "status": "ready", "chunk_count": 2}]}
    payloads = {
        "list_knowledge_documents": overview,
        "search_knowledge": {"results": [dict(HIT)], "result_count": 1},
        "read_knowledge_chunks": {"chunks": []},
    }
    return {name: {"spec": {"name": name, "description": name,
                            "parameters": {"type": "object", "properties": {}}},
                   "callable": AsyncMock(return_value=payload)}
            for name, payload in payloads.items()}


def model_context(client):
    context = AsyncMock()
    context.__aenter__.return_value = client
    return context


class RetrievalLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_contains_actual_origin_and_verified_quote(self):
        tools = fake_tools()
        client = fake_client([
            response(tool_call("search_knowledge", {"query": "SS-TEST-042 采样率和阈值"})),
            assessment(),
        ])
        result = await RetrievalRunner(client, tools, model="test-model").run("演示实验的参数是什么？")
        self.assertEqual(result["status"], "sufficient")
        self.assertEqual(result["stop_reason"], "evidence_sufficient")
        source = result["evidence"][0]
        self.assertEqual(source["quote"], QUOTE)
        self.assertIn(source["quote"], source["text"])
        self.assertEqual(source["filename"], FILENAME)
        self.assertEqual(source["document_id"], "doc-1")
        self.assertIn("第 3 页", source["citation"])
        self.assertEqual(result["attempts"], {"model_turns": 2, "searches": 1, "tool_calls": 2, "mcp_calls": 2})
        self.assertFalse(result["knowledge_graph_used"])

    async def test_repeated_document_listing_reuses_only_the_task_preflight_snapshot(self):
        tools = fake_tools()
        client = fake_client([
            response(tool_call("list_knowledge_documents", {}, "l1")),
            response(tool_call("search_knowledge", {"query": "SS-TEST-042 参数"}, "q1")),
            assessment(),
        ])
        result = await RetrievalRunner(client, tools, model="test").run("演示参数是多少？")
        self.assertEqual(result["status"], "sufficient")
        tools["list_knowledge_documents"]["callable"].assert_awaited_once()
        self.assertFalse(result["tool_evidence"][0]["cached"])
        self.assertTrue(result["tool_evidence"][1]["cached"])
        self.assertEqual(result["attempts"]["tool_calls"], 3)
        self.assertEqual(result["attempts"]["mcp_calls"], 2)

    async def test_cached_document_listing_still_consumes_the_tool_budget(self):
        tools = fake_tools()
        client = fake_client([
            response(tool_call("list_knowledge_documents", {}, "l1")),
            response(tool_call("search_knowledge", {"query": "实验参数"}, "q1")),
        ])
        result = await RetrievalRunner(client, tools, model="test", max_tool_calls=2).run("查参数")
        self.assertEqual(result["stop_reason"], "max_tool_calls")
        self.assertEqual(result["attempts"]["tool_calls"], 2)
        self.assertEqual(result["attempts"]["mcp_calls"], 1)
        tools["list_knowledge_documents"]["callable"].assert_awaited_once()
        tools["search_knowledge"]["callable"].assert_not_awaited()

    async def test_document_snapshot_is_not_shared_with_another_task(self):
        tools = fake_tools()
        for _ in range(2):
            client = fake_client([
                response(tool_call("list_knowledge_documents", {}, "l1")),
                assessment("insufficient_evidence", evidence=[], missing=["还未查询相关证据"]),
            ])
            result = await RetrievalRunner(client, tools, model="test").run("查资料")
            self.assertEqual(result["attempts"]["mcp_calls"], 1)
        self.assertEqual(tools["list_knowledge_documents"]["callable"].await_count, 2)

    async def test_failed_document_listing_is_not_cached(self):
        tools = fake_tools()
        tools["list_knowledge_documents"]["callable"].return_value = {"error": "服务暂时不可用"}
        runner = RetrievalRunner(fake_client(), tools, model="test")
        for _ in range(2):
            self.assertIn("error", await runner.call("list_knowledge_documents", {}))
        self.assertIsNone(runner._overview)
        self.assertEqual(tools["list_knowledge_documents"]["callable"].await_count, 2)
        self.assertEqual(runner.result("failed", "retrieval_unavailable", "失败")["attempts"]["mcp_calls"], 2)

    async def test_forged_chunk_or_quote_cannot_claim_sufficient(self):
        for reference in ({"chunk_id": "invented", "quote": QUOTE},
                          {"chunk_id": "chunk-1", "quote": "阈值为 0.99"}):
            with self.subTest(reference=reference):
                client = fake_client([
                    response(tool_call("search_knowledge", {"query": "实验参数"})),
                    assessment(evidence=[reference]),
                ])
                result = await RetrievalRunner(client, fake_tools(), model="test", max_turns=2).run("参数是多少？")
                self.assertEqual(result["status"], "insufficient_evidence")
                self.assertEqual(result["stop_reason"], "invalid_evidence")
                self.assertEqual(result["evidence"], [])

    async def test_high_similarity_does_not_fill_missing_formula_and_query_is_rewritten(self):
        tools = fake_tools()
        client = fake_client([
            response(tool_call("search_knowledge", {"query": "能量检测阈值"}, "q1")),
            response(tool_call("search_knowledge", {"query": "能量检测通用阈值公式及噪声条件"}, "q2")),
            assessment("insufficient_evidence", missing=["缺少通用阈值公式和适用噪声条件"],
                       summary="只有演示参数，不能据此编写通用算法。"),
        ])
        result = await RetrievalRunner(client, tools, model="test").run(
            "查通用阈值计算方法", acceptance="必须包含公式和适用条件")
        self.assertEqual(result["status"], "insufficient_evidence")
        self.assertEqual(result["stop_reason"], "evidence_insufficient")
        self.assertEqual(result["attempts"]["searches"], 2)
        queries = [call.kwargs["query"] for call in tools["search_knowledge"]["callable"].await_args_list]
        self.assertEqual(len(set(queries)), 2)
        self.assertEqual(result["evidence"][0]["score"], 0.99)
        self.assertIn("公式", result["missing_information"][0])

    async def test_three_search_budget_blocks_a_fourth_embedding_request(self):
        tools = fake_tools()
        client = fake_client([response(tool_call("search_knowledge", {"query": f"查询 {i}"}, f"q{i}"))
                              for i in range(4)])
        result = await RetrievalRunner(client, tools, model="test", max_searches=3).run("查资料")
        self.assertEqual(result["stop_reason"], "max_searches")
        self.assertEqual(result["status"], "insufficient_evidence")
        self.assertEqual(result["attempts"]["searches"], 3)
        self.assertEqual(tools["search_knowledge"]["callable"].await_count, 3)
        self.assertTrue(result["evidence"])

    async def test_model_turn_budget_stops_without_claiming_completion(self):
        client = fake_client([
            response(tool_call("search_knowledge", {"query": "查询 1"}, "q1")),
            response(tool_call("search_knowledge", {"query": "查询 2"}, "q2")),
        ])
        result = await RetrievalRunner(client, fake_tools(), model="test", max_turns=2).run("查资料")
        self.assertEqual(result["stop_reason"], "max_model_turns")
        self.assertEqual(result["status"], "insufficient_evidence")
        self.assertEqual(client.messages.create.await_count, 2)
        self.assertTrue(result["evidence"])

    async def test_tool_budget_includes_preflight_and_prevents_context_read(self):
        tools = fake_tools()
        client = fake_client([
            response(tool_call("search_knowledge", {"query": "查询"}, "q1")),
            response(tool_call("read_knowledge_chunks", {"document_id": "doc-1"}, "r1")),
        ])
        result = await RetrievalRunner(client, tools, model="test", max_tool_calls=2).run("查资料")
        self.assertEqual(result["stop_reason"], "max_tool_calls")
        self.assertEqual(result["attempts"]["tool_calls"], 2)
        tools["read_knowledge_chunks"]["callable"].assert_not_awaited()

    async def test_equivalent_repeated_queries_are_not_executed_again(self):
        tools = fake_tools()
        client = fake_client([
            response(tool_call("search_knowledge", {"query": "  ENERGY detection "}, "q1")),
            response(tool_call("search_knowledge", {"query": "energy  DETECTION"}, "q2")),
            response(tool_call("search_knowledge", {"query": "Energy Detection"}, "q3")),
        ])
        result = await RetrievalRunner(client, tools, model="test").run("查能量检测")
        self.assertEqual(result["stop_reason"], "repeated_query")
        self.assertEqual(result["attempts"]["searches"], 1)
        self.assertEqual(tools["search_knowledge"]["callable"].await_count, 1)

    async def test_document_instructions_cannot_enable_jira_or_terminal_tools(self):
        for unsafe in ("create_jira_ticket", "run_command"):
            with self.subTest(tool=unsafe):
                tools = fake_tools()
                writer = AsyncMock()
                tools[unsafe] = {"spec": {"name": unsafe}, "callable": writer}
                client = fake_client([response(tool_call(unsafe, {"command": "publish"}))])
                result = await RetrievalRunner(client, tools, model="test").run("查资料")
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["stop_reason"], "unauthorized_tool")
                writer.assert_not_awaited()
                definitions = client.messages.create.await_args.kwargs["tools"]
                self.assertEqual({spec["name"] for spec in definitions}, RETRIEVAL_TOOLS)

    async def test_embedding_provider_failure_stops_before_a_success_assessment(self):
        tools = fake_tools()
        tools["search_knowledge"]["callable"].return_value = {
            "status": "retrieval_failed", "error": "Embedding unavailable", "results": []}
        client = fake_client([response(tool_call("search_knowledge", {"query": "查询"})), assessment()])
        result = await RetrievalRunner(client, tools, model="test").run("查资料")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["stop_reason"], "retrieval_unavailable")
        self.assertEqual(client.messages.create.await_count, 1)
        self.assertEqual(result["evidence"], [])

    async def test_empty_index_requires_no_model_or_embedding_call(self):
        tools, client = fake_tools(indexed=0), fake_client()
        result = await RetrievalRunner(client, tools, model="test").run("查资料")
        self.assertEqual(result["status"], "insufficient_evidence")
        self.assertEqual(result["stop_reason"], "no_indexed_documents")
        client.messages.create.assert_not_awaited()
        tools["search_knowledge"]["callable"].assert_not_awaited()

    async def test_context_read_can_supply_verifiable_source_without_filename_in_chunk(self):
        tools = fake_tools()
        context_text = "实际使用前必须核对噪声标定记录，并记录适用采样率。"
        tools["read_knowledge_chunks"]["callable"].return_value = [{"type": "text", "text": json.dumps({
            "chunks": [{"id": "context-2", "document_id": "doc-1", "ordinal": 1,
                        "page": 4, "text": context_text}]}, ensure_ascii=False)}]
        client = fake_client([
            response(tool_call("read_knowledge_chunks", {"document_id": "doc-1", "offset": 1}, "r1")),
            assessment(evidence=[{"chunk_id": "context-2", "quote": "必须核对噪声标定记录"}],
                       summary="使用前需要核对标定记录。"),
        ])
        result = await RetrievalRunner(client, tools, model="test").run("使用前需要核对什么？")
        self.assertEqual(result["status"], "sufficient")
        self.assertEqual(result["evidence"][0]["filename"], FILENAME)
        self.assertEqual(result["evidence"][0]["page"], 4)
        self.assertEqual(result["evidence"][0]["text"], context_text)
        self.assertEqual(result["attempts"]["searches"], 0)

    async def test_missing_authorized_tool_fails_before_any_tool_or_model_use(self):
        tools, client = fake_tools(), fake_client()
        del tools["read_knowledge_chunks"]
        result = await RetrievalRunner(client, tools, model="test").run("查资料")
        self.assertEqual(result["stop_reason"], "missing_authorized_tools")
        self.assertEqual(result["status"], "failed")
        client.messages.create.assert_not_awaited()
        tools["list_knowledge_documents"]["callable"].assert_not_awaited()

    async def test_incomplete_model_output_is_not_parsed_as_final_success(self):
        client = fake_client([response(NS(type="text", text='{"status":"sufficient"'),
                                       stop_reason="max_tokens")])
        result = await RetrievalRunner(client, fake_tools(), model="test").run("查资料")
        self.assertEqual(result["status"], "insufficient_evidence")
        self.assertEqual(result["stop_reason"], "incomplete_assessment")

    async def test_malformed_evidence_containers_produce_a_safe_insufficient_report(self):
        runner = RetrievalRunner(fake_client(), fake_tools(), model="test")
        runner.remember("search_knowledge", {"results": [dict(HIT)]})
        for evidence in (None, 42, "chunk-1", {"chunk_id": "chunk-1", "quote": QUOTE}):
            with self.subTest(evidence=evidence):
                result = runner.assess(json.dumps({"status": "sufficient", "summary": "未经核对的结论",
                                                   "evidence": evidence, "missing_information": []}))
                self.assertEqual(result["status"], "insufficient_evidence")
                self.assertEqual(result["stop_reason"], "invalid_assessment")

    async def test_single_assessment_with_surrounding_prose_is_parsed_and_verified(self):
        report = assessment().content[0].text
        text = "已检索到演示实验的参数，下面给出判断。\n" + report + "\n这些参数仅用于演示。"
        parsed = parse_assessment(text)
        self.assertEqual(parsed["status"], "sufficient")
        self.assertEqual(parsed["evidence"][0]["quote"], QUOTE)
        client = fake_client([
            response(tool_call("search_knowledge", {"query": "SS-TEST-042 参数"})),
            response(NS(type="text", text=text)),
        ])
        result = await RetrievalRunner(client, fake_tools(), model="test").run("演示参数是多少？")
        self.assertEqual(result["status"], "sufficient")
        self.assertEqual(result["evidence"][0]["chunk_id"], "chunk-1")
        self.assertIn(result["evidence"][0]["quote"], result["evidence"][0]["text"])

    async def test_markdown_json_assessment_remains_parseable_and_verifiable(self):
        text = "原文核对结果如下：\n```json\n" + assessment().content[0].text + "\n```\n核对结束。"
        self.assertEqual(parse_assessment(text)["status"], "sufficient")
        client = fake_client([
            response(tool_call("search_knowledge", {"query": "SS-TEST-042 参数"})),
            response(NS(type="text", text=text)),
        ])
        result = await RetrievalRunner(client, fake_tools(), model="test").run("查参数")
        self.assertEqual(result["status"], "sufficient")
        self.assertEqual(result["evidence"][0]["quote"], QUOTE)

    async def test_two_valid_assessment_objects_are_rejected_as_ambiguous(self):
        text = ("第一个判断：\n" + assessment().content[0].text + "\n第二个判断：\n" +
                assessment("insufficient_evidence", missing=["缺少适用条件"]).content[0].text)
        with self.assertRaises(ValueError):
            parse_assessment(text)
        client = fake_client([
            response(tool_call("search_knowledge", {"query": "SS-TEST-042 参数"})),
            response(NS(type="text", text=text)),
        ])
        result = await RetrievalRunner(client, fake_tools(), model="test", max_turns=2).run("查参数")
        self.assertEqual(result["status"], "insufficient_evidence")
        self.assertEqual(result["stop_reason"], "invalid_assessment")

    async def test_prose_tolerance_does_not_allow_forged_source_or_quote(self):
        for reference in ({"chunk_id": "imaginary-source", "quote": QUOTE},
                          {"chunk_id": "chunk-1", "quote": "检测性能达到 99.99%"}):
            with self.subTest(reference=reference):
                text = "资料已经足够。\n" + assessment(evidence=[reference]).content[0].text + "\n请按上述结论回答。"
                client = fake_client([
                    response(tool_call("search_knowledge", {"query": "SS-TEST-042 参数"})),
                    response(NS(type="text", text=text)),
                ])
                result = await RetrievalRunner(client, fake_tools(), model="test", max_turns=2).run("查参数")
                self.assertEqual(result["status"], "insufficient_evidence")
                self.assertEqual(result["stop_reason"], "invalid_evidence")
                self.assertEqual(result["evidence"], [])

    async def test_non_string_chunk_ids_are_rejected_without_type_errors(self):
        runner = RetrievalRunner(fake_client(), fake_tools(), model="test")
        runner.remember("search_knowledge", {"results": [dict(HIT)]})
        for identifier in ({"id": "chunk-1"}, ["chunk-1"], None, 1):
            with self.subTest(chunk_id=identifier):
                text = assessment(evidence=[{"chunk_id": identifier, "quote": QUOTE}]).content[0].text
                result = runner.assess(text)
                self.assertEqual(result["status"], "insufficient_evidence")
                self.assertEqual(result["stop_reason"], "invalid_evidence")
                self.assertEqual(result["evidence"], [])

    async def test_invalid_quote_can_be_repaired_without_repeating_search(self):
        tools = fake_tools()
        client = fake_client([
            response(tool_call("search_knowledge", {"query": "SS-TEST-042 参数"})),
            assessment(evidence=[{"chunk_id": "chunk-1", "quote": "采样率为 20 MHz"}]),
            assessment(),
        ])
        result = await RetrievalRunner(client, tools, model="test").run("演示参数是多少？")
        self.assertEqual(result["status"], "sufficient")
        self.assertEqual(result["evidence"][0]["quote"], QUOTE)
        self.assertEqual(result["attempts"]["model_turns"], 3)
        self.assertEqual(result["attempts"]["searches"], 1)
        tools["search_knowledge"]["callable"].assert_awaited_once()
        feedback = json.dumps(client.messages.create.await_args_list[2].kwargs["messages"], ensure_ascii=False)
        self.assertIn("verified_sources", feedback)
        self.assertIn("chunk-1", feedback)
        self.assertIn(TEXT, feedback)

    async def test_two_failed_repair_attempts_stop_with_invalid_evidence(self):
        tools = fake_tools()
        invalid = {"chunk_id": "chunk-1", "quote": "检测性能达到 99.99%"}
        client = fake_client([
            response(tool_call("search_knowledge", {"query": "SS-TEST-042 参数"})),
            assessment(evidence=[invalid]),
            assessment(evidence=[invalid]),
            assessment(evidence=[invalid]),
        ])
        result = await RetrievalRunner(client, tools, model="test", max_turns=5).run("演示参数是多少？")
        self.assertEqual(result["status"], "insufficient_evidence")
        self.assertEqual(result["stop_reason"], "invalid_evidence")
        self.assertEqual(client.messages.create.await_count, 4)
        self.assertLessEqual(result["attempts"]["model_turns"], 5)
        self.assertEqual(result["attempts"]["searches"], 1)
        tools["search_knowledge"]["callable"].assert_awaited_once()
        self.assertEqual(result["evidence"], [])

    async def test_repair_does_not_exceed_remaining_model_turn_budget(self):
        tools = fake_tools()
        client = fake_client([
            response(tool_call("search_knowledge", {"query": "SS-TEST-042 参数"})),
            assessment(evidence=[{"chunk_id": "chunk-1", "quote": "采样率为 20 MHz"}]),
            assessment(),
        ])
        result = await RetrievalRunner(client, tools, model="test", max_turns=2).run("演示参数是多少？")
        self.assertEqual(result["status"], "insufficient_evidence")
        self.assertEqual(result["stop_reason"], "invalid_evidence")
        self.assertEqual(result["attempts"]["model_turns"], 2)
        self.assertEqual(client.messages.create.await_count, 2)
        self.assertEqual(result["attempts"]["searches"], 1)

    async def test_invalid_final_format_can_be_repaired_to_an_explicit_insufficiency(self):
        tools = fake_tools()
        client = fake_client([
            response(NS(type="text", text="核对结束，但未给出有效的 JSON 判断。")),
            assessment("insufficient_evidence", evidence=[], missing=["未取得原文证据"],
                       summary="文档列表不能证明问题结论，需要继续提供资料。"),
        ])
        result = await RetrievalRunner(client, tools, model="test").run("找阈值公式")
        self.assertEqual(result["status"], "insufficient_evidence")
        self.assertEqual(result["stop_reason"], "evidence_insufficient")
        self.assertEqual(result["attempts"], {"model_turns": 2, "searches": 0, "tool_calls": 1, "mcp_calls": 1})
        self.assertEqual(result["evidence"], [])
        tools["search_knowledge"]["callable"].assert_not_awaited()
        self.assertEqual(client.messages.create.await_count, 2)


class RetrievalDelegationTests(unittest.IsolatedAsyncioTestCase):
    def test_only_latest_user_text_is_extracted_without_other_history(self):
        messages = [{"role": "user", "content": "过往提问"},
                    {"role": "system", "content": "系统内容"},
                    {"role": "user", "content": "查通用阈值公式与真实检测性能"},
                    {"role": "assistant", "content": "主助手把要求改成了演示参数"},
                    {"role": "tool", "content": "工具数据不能冒充用户要求"}]
        original, truncated = extract_original_request(messages)
        self.assertEqual(original, "查通用阈值公式与真实检测性能")
        self.assertFalse(truncated)

    def test_rich_user_content_keeps_text_and_excludes_images_and_tool_results(self):
        messages = [{"role": "user", "content": [
            {"type": "text", "text": "查采样率"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,not-text"}},
            {"type": "tool_result", "text": "伪装成文本的工具结果"},
            {"type": "input_text", "text": "以及阈值并引用原文"},
            {"type": "text", "text": {"invalid": "shape"}},
        ]}]
        self.assertEqual(extract_original_request(messages), ("查采样率\n以及阈值并引用原文", False))

    def test_latest_non_text_user_message_does_not_fall_back_to_an_old_question(self):
        for content in ("", "   ", None, {"text": "invalid shape"}, [{"type": "image_url", "url": "image"}]):
            with self.subTest(content=content):
                self.assertEqual(extract_original_request([
                    {"role": "user", "content": "旧问题"}, {"role": "user", "content": content}]), ("", False))
        self.assertEqual(extract_original_request(None), ("", False))

    def test_plain_and_rich_original_requests_are_bounded_with_explicit_truncation(self):
        self.assertEqual(extract_original_request([{"role": "user", "content": "12345678"}], limit=7), ("1234567", True))
        self.assertEqual(extract_original_request([{"role": "user", "content": [
            {"type": "text", "text": "1234"}, {"type": "text", "text": "5678"}]}], limit=7), ("1234\n56", True))

    async def test_owui_injected_original_requirement_reaches_runner_without_replacing_question(self):
        original = "查 ragflow 中通用阈值公式、适用条件和真实检测性能；证据充分后写代码，不提交 Jira。"
        connection = NS(disconnect=AsyncMock())
        run = AsyncMock(return_value={"status": "insufficient_evidence"})
        with patch("supervisor.retrieval_tool.connect_authorized_knowledge", AsyncMock(return_value=(connection, fake_tools()))), \
             patch("supervisor.retrieval_tool.AsyncAnthropic", return_value=model_context(fake_client())), \
             patch.object(RetrievalRunner, "run", run):
            await Tools().delegate_retrieval_task("演示参数", context="任务上下文", acceptance="找到演示条目",
                __request__=object(), __user__={"id": "owner"}, __messages__=[
                    {"role": "user", "content": "历史用户问题"}, {"role": "system", "content": "不可传给任务的系统文本"},
                    {"role": "user", "content": original}, {"role": "assistant", "content": "被简化的问题"}])
        question, context, acceptance = run.await_args.args
        payload = json.loads(context)
        self.assertEqual(question, "演示参数")
        self.assertEqual(acceptance, "找到演示条目")
        self.assertEqual(payload["original_user_request"], original)
        self.assertEqual(payload["task_context"], "任务上下文")
        self.assertNotIn("历史用户问题", context)
        self.assertNotIn("不可传给任务的系统文本", context)
        self.assertNotIn("被简化的问题", context)
        connection.disconnect.assert_awaited_once()

    async def test_original_request_diagnostic_does_not_return_the_user_text(self):
        original = "查采样率与阈值并引用原文"
        client = fake_client([response(tool_call("search_knowledge", {"query": "演示参数"})), assessment()])
        connection = NS(disconnect=AsyncMock())
        with patch("supervisor.retrieval_tool.connect_authorized_knowledge", AsyncMock(return_value=(connection, fake_tools()))), \
             patch("supervisor.retrieval_tool.AsyncAnthropic", return_value=model_context(client)):
            result = await Tools().delegate_retrieval_task("查资料", __request__=object(), __user__={"id": "owner"},
                __messages__=[{"role": "user", "content": original}])
        self.assertTrue(result["original_request_present"])
        self.assertEqual(result["original_request_chars"], len(original))
        self.assertFalse(result["original_request_truncated"])
        self.assertNotIn(original, json.dumps(result, ensure_ascii=False))

    async def test_direct_delegation_without_injected_messages_keeps_the_original_context(self):
        connection = NS(disconnect=AsyncMock())
        run = AsyncMock(return_value={"status": "insufficient_evidence"})
        with patch("supervisor.retrieval_tool.connect_authorized_knowledge", AsyncMock(return_value=(connection, fake_tools()))), \
             patch("supervisor.retrieval_tool.AsyncAnthropic", return_value=model_context(fake_client())), \
             patch.object(RetrievalRunner, "run", run):
            await Tools().delegate_retrieval_task("查公式", context="业务上下文", acceptance="需要真实公式",
                __request__=object(), __user__={"id": "owner"})
        run.assert_awaited_once_with("查公式", "业务上下文", "需要真实公式")

    async def test_oversized_original_requirement_cannot_claim_coverage_of_a_truncated_request(self):
        connect, factory = AsyncMock(), Mock()
        with patch("supervisor.retrieval_tool.connect_authorized_knowledge", connect), \
             patch("supervisor.retrieval_tool.AsyncAnthropic", factory):
            result = await Tools().delegate_retrieval_task("查资料", __messages__=[{"role": "user", "content": "x" * 12001}])
        self.assertEqual(result["status"], "insufficient_evidence")
        self.assertEqual(result["stop_reason"], "original_request_too_long")
        self.assertTrue(result["original_request_truncated"])
        self.assertEqual(result["original_request_chars"], 12000)
        connect.assert_not_awaited()
        factory.assert_not_called()

    async def test_access_denial_never_creates_model_client(self):
        connect = AsyncMock(side_effect=PermissionError("当前用户无读取权限。"))
        factory = Mock()
        with patch("supervisor.retrieval_tool.connect_authorized_knowledge", connect), \
             patch("supervisor.retrieval_tool.AsyncAnthropic", factory):
            result = await Tools().delegate_retrieval_task("查资料", __request__=object(), __user__={"id": "denied"})
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["stop_reason"], "access_denied")
        factory.assert_not_called()

    async def test_model_failure_preserves_observed_sources_and_closes_connection(self):
        client = fake_client([response(tool_call("search_knowledge", {"query": "演示参数"})),
                              RuntimeError("private-provider-details")])
        connection = NS(disconnect=AsyncMock())
        with patch("supervisor.retrieval_tool.connect_authorized_knowledge", AsyncMock(return_value=(connection, fake_tools()))), \
             patch("supervisor.retrieval_tool.AsyncAnthropic", return_value=model_context(client)):
            result = await Tools().delegate_retrieval_task("查参数", __request__=object(), __user__={"id": "owner"})
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["stop_reason"], "agent_unavailable")
        self.assertEqual(result["evidence"][0]["chunk_id"], "chunk-1")
        self.assertNotIn("private-provider-details", json.dumps(result))
        connection.disconnect.assert_awaited_once()

    async def test_timeout_keeps_sources_and_disconnects_in_the_connection_task(self):
        task_ids, calls = [], 0
        async def create(**kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                return response(tool_call("search_knowledge", {"query": "演示参数"}))
            await asyncio.Event().wait()
        async def disconnect():
            task_ids.append(asyncio.current_task())
        connection = NS(disconnect=AsyncMock(side_effect=disconnect))
        async def connect(*args):
            task_ids.append(asyncio.current_task())
            return connection, fake_tools()
        client = NS(messages=NS(create=AsyncMock(side_effect=create)))
        delegate = Tools()
        delegate.valves.TIMEOUT_SECONDS = 1
        with patch("supervisor.retrieval_tool.connect_authorized_knowledge", AsyncMock(side_effect=connect)), \
             patch("supervisor.retrieval_tool.AsyncAnthropic", return_value=model_context(client)):
            result = await delegate.delegate_retrieval_task("查参数", __request__=object(), __user__={"id": "owner"})
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["stop_reason"], "timeout")
        self.assertEqual(result["evidence"][0]["chunk_id"], "chunk-1")
        self.assertEqual(result["attempts"]["searches"], 1)
        connection.disconnect.assert_awaited_once()
        self.assertIs(task_ids[0], task_ids[1])

    async def test_disconnect_error_does_not_discard_a_completed_evidence_report(self):
        client = fake_client([response(tool_call("search_knowledge", {"query": "演示参数"})), assessment()])
        connection = NS(disconnect=AsyncMock(side_effect=RuntimeError("connection already closed")))
        with patch("supervisor.retrieval_tool.connect_authorized_knowledge", AsyncMock(return_value=(connection, fake_tools()))), \
             patch("supervisor.retrieval_tool.AsyncAnthropic", return_value=model_context(client)):
            result = await Tools().delegate_retrieval_task("查参数", __request__=object(), __user__={"id": "owner"})
        self.assertEqual(result["status"], "sufficient")
        self.assertEqual(result["evidence"][0]["quote"], QUOTE)
        connection.disconnect.assert_awaited_once()


class KnowledgeConnectionAuthorizationTests(unittest.IsolatedAsyncioTestCase):
    def authorization_modules(self, *, enabled=True, authorized=True, filters=""):
        connection = {"type": "mcp", "info": {"id": "ragflow-knowledge"},
                      "url": "http://knowledge.invalid/mcp",
                      "config": {"enable": enabled, "function_name_filter_list": filters}}
        modules = {name: ModuleType(name) for name in (
            "open_webui", "open_webui.models", "open_webui.models.config",
            "open_webui.models.users", "open_webui.utils", "open_webui.utils.access_control",
            "open_webui.utils.tools", "open_webui.utils.misc", "open_webui.utils.mcp",
            "open_webui.utils.mcp.client")}
        config = NS(get=AsyncMock(return_value=[connection]))
        users = NS(get_user_by_id=AsyncMock(return_value=NS(id="owner")))
        specs = [{"name": name, "parameters": {"type": "object", "properties": {}}}
                 for name in sorted(RETRIEVAL_TOOLS)] + [{"name": "delete_document"}]
        client = NS(call_tool=AsyncMock(return_value={"results": []}), connect=AsyncMock(),
                    list_tool_specs=AsyncMock(return_value=specs), disconnect=AsyncMock())
        access = AsyncMock(return_value=authorized)
        headers = AsyncMock(return_value=({"Authorization": "Bearer fake-test-token"}, {}))
        factory = Mock(return_value=client)
        modules["open_webui.models.config"].Config = config
        modules["open_webui.models.users"].Users = users
        modules["open_webui.utils.access_control"].has_connection_access = access
        modules["open_webui.utils.tools"].build_tool_server_headers = headers
        modules["open_webui.utils.mcp.client"].MCPClient = factory
        modules["open_webui.utils.misc"].is_string_allowed = (
            lambda name, patterns: any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns))
        return NS(modules=modules, users=users, client=client, access=access,
                  headers=headers, factory=factory, connection=connection)

    async def test_missing_authentication_stops_before_resolving_mcp(self):
        fixture = self.authorization_modules()
        with patch.dict(sys.modules, fixture.modules):
            with self.assertRaises(PermissionError):
                await connect_authorized_knowledge(None, {"id": "owner"}, {}, "ragflow-knowledge")
        fixture.users.get_user_by_id.assert_not_awaited()
        fixture.access.assert_not_awaited()
        fixture.factory.assert_not_called()

    async def test_disabled_connection_is_not_opened(self):
        fixture = self.authorization_modules(enabled=False)
        with patch.dict(sys.modules, fixture.modules):
            with self.assertRaises(PermissionError):
                await connect_authorized_knowledge(object(), {"id": "owner"}, {}, "ragflow-knowledge")
        fixture.access.assert_not_awaited()
        fixture.factory.assert_not_called()

    async def test_owui_permission_denial_is_preserved(self):
        fixture = self.authorization_modules(authorized=False)
        with patch.dict(sys.modules, fixture.modules):
            with self.assertRaises(PermissionError):
                await connect_authorized_knowledge(object(), {"id": "owner"}, {}, "ragflow-knowledge")
        fixture.access.assert_awaited_once()
        fixture.headers.assert_not_awaited()
        fixture.factory.assert_not_called()

    async def test_authorized_connection_filters_writes_and_preserves_user_context(self):
        fixture = self.authorization_modules()
        request, metadata, user_dict = object(), {"chat_id": "chat-a"}, {"id": "owner"}
        with patch.dict(sys.modules, fixture.modules):
            returned, tools = await connect_authorized_knowledge(request, user_dict, metadata, "ragflow-knowledge")
            await tools["search_knowledge"]["callable"](query="SS-TEST-042", top_k=2)
        self.assertIs(returned, fixture.client)
        self.assertEqual(set(tools), RETRIEVAL_TOOLS)
        fixture.access.assert_awaited_once_with(fixture.users.get_user_by_id.return_value, fixture.connection)
        fixture.headers.assert_awaited_once_with(fixture.connection, request, fixture.users.get_user_by_id.return_value,
            server_id="ragflow-knowledge", metadata=metadata,
            extra_params={"__user__": user_dict, "__metadata__": metadata})
        fixture.client.connect.assert_awaited_once_with("http://knowledge.invalid/mcp",
                                                       headers={"Authorization": "Bearer fake-test-token"})
        fixture.client.call_tool.assert_awaited_once_with("search_knowledge", {"query": "SS-TEST-042", "top_k": 2})

    async def test_connection_function_filter_is_respected(self):
        fixture = self.authorization_modules(filters="search_knowledge")
        with patch.dict(sys.modules, fixture.modules):
            _, tools = await connect_authorized_knowledge(object(), {"id": "owner"}, {}, "ragflow-knowledge")
        self.assertEqual(set(tools), {"search_knowledge"})

    async def test_list_specs_failure_disconnects_in_the_same_task(self):
        fixture = self.authorization_modules()
        tasks = []
        async def connect(*args, **kwargs):
            tasks.append(asyncio.current_task())
        async def disconnect():
            tasks.append(asyncio.current_task())
        fixture.client.connect.side_effect = connect
        fixture.client.disconnect.side_effect = disconnect
        fixture.client.list_tool_specs.side_effect = RuntimeError("spec listing failed")
        with patch.dict(sys.modules, fixture.modules):
            with self.assertRaisesRegex(RuntimeError, "spec listing failed"):
                await connect_authorized_knowledge(object(), {"id": "owner"}, {}, "ragflow-knowledge")
        fixture.client.disconnect.assert_awaited_once()
        self.assertIs(tasks[0], tasks[1])


if __name__ == "__main__":
    unittest.main()
