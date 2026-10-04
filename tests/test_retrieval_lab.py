import asyncio
import io
import importlib
import json
import os
from pathlib import Path
import tempfile
import unittest
import zipfile
from unittest.mock import patch

import httpx
import numpy as np

from retrieval_lab.embeddings import BailianEmbedding, EmbeddingError, Settings, SettingsStore, normalized_vector
from retrieval_lab.store import Store, extract_pages, split_pages


class FakeEmbedding:
    """Only tests use these deterministic vectors; the service always calls Bailian."""
    def __init__(self, dimensions=256, fail=False):
        self.settings = Settings(dimensions=dimensions, api_key="test-only")
        self.calls, self.fail = [], fail

    async def embed(self, texts):
        self.calls.append(texts)
        if self.fail:
            raise EmbeddingError("test provider failure")
        result = []
        for text in texts:
            vector = np.zeros(self.settings.dimensions, dtype=np.float32)
            vector[0 if "能量" in text else 1] = 1
            result.append(vector)
        return result


class RetrievalTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = Store(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    async def test_real_store_persists_deduplicates_and_retrieves_citations(self):
        first = self.store.ingest("能量检测.md", "能量检测实验条件。".encode())
        duplicate = self.store.ingest("不同文件名.md", "能量检测实验条件。".encode())
        self.assertEqual(first["id"], duplicate["id"])
        self.assertTrue(duplicate["duplicate"])
        other = self.store.ingest("匹配滤波.md", "匹配滤波实验条件。".encode())
        embedding = FakeEmbedding()
        await self.store.index(first["id"], embedding)
        await self.store.index(other["id"], embedding)
        restored = Store(self.temporary.name)
        query = normalized_vector([1] + [0] * 255, 256)
        result = restored.retrieve("能量", query, embedding.settings.profile, min_score=0.5)
        self.assertEqual(result["result_count"], 1)
        self.assertIn("能量检测.md", result["results"][0]["citation"])
        self.assertEqual(result["results"][0]["page"], 1)

    async def test_retry_reuses_successful_embedding_cache(self):
        document = self.store.ingest("文档.md", "能量检测。".encode())
        embedding = FakeEmbedding()
        await self.store.index(document["id"], embedding)
        await self.store.index(document["id"], embedding)
        self.assertEqual(len(embedding.calls), 1)

    async def test_failed_partial_index_is_not_searchable(self):
        document = self.store.ingest("文档.md", "能量".encode())
        with self.store.connect() as db:
            db.execute("UPDATE chunks SET vector=?,profile=?", (np.ones(256, dtype=np.float32).tobytes(), FakeEmbedding().settings.profile))
        failing = FakeEmbedding(fail=True)
        with self.assertRaises(EmbeddingError):
            await self.store.index(document["id"], failing)
        self.assertEqual(self.store.overview(failing.settings.profile)["indexed_chunk_count"], 0)
        result = self.store.retrieve("能量", np.ones(256, dtype=np.float32), failing.settings.profile)
        self.assertEqual(result["result_count"], 0)

    async def test_changed_model_profile_needs_reindex(self):
        document = self.store.ingest("文档.md", "能量检测".encode())
        original = FakeEmbedding()
        await self.store.index(document["id"], original)
        different = FakeEmbedding(dimensions=512)
        self.assertEqual(self.store.overview(different.settings.profile)["documents"][0]["status"], "needs_reindex")
        self.assertEqual(self.store.retrieve("能量", np.ones(512, dtype=np.float32), different.settings.profile)["result_count"], 0)

    async def test_document_scope_and_delete(self):
        first = self.store.ingest("文档一.md", "能量一".encode())
        second = self.store.ingest("文档二.md", "能量二".encode())
        embedding = FakeEmbedding()
        await self.store.index(first["id"], embedding)
        await self.store.index(second["id"], embedding)
        result = self.store.retrieve("能量", (await embedding.embed(["能量"]))[0], embedding.settings.profile, document_id=second["id"])
        self.assertEqual([h["document_id"] for h in result["results"]], [second["id"]])
        self.store.delete(second["id"])
        with self.assertRaises(ValueError):
            self.store.chunks(second["id"])

    def test_split_preserves_pages_and_covers_end(self):
        pages = [(3, "中文测试。" * 400), (4, "第二页原文")]
        chunks = split_pages(pages)
        self.assertEqual(chunks[0]["page"], 3)
        self.assertEqual(chunks[-1]["page"], 4)
        self.assertTrue(all(len(c["text"]) <= 900 for c in chunks))

    def test_docx_text_extraction_without_heavy_office_runtime(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>频谱感知</w:t></w:r></w:p></w:body></w:document>')
        self.assertIn("频谱感知", extract_pages("论文.docx", buffer.getvalue())[0][1])

    async def test_provider_batches_orders_results_and_normalizes(self):
        batches = []
        def handler(request):
            body = json.loads(request.content)
            batches.append(body["input"])
            return httpx.Response(200, json={"data": [{"index": i, "embedding": [2] + [0] * 255}
                for i in reversed(range(len(body["input"])))]})
        provider = BailianEmbedding(Settings(dimensions=256, api_key="test-only"), httpx.MockTransport(handler))
        result = await provider.embed([str(i) for i in range(23)])
        self.assertEqual([len(b) for b in batches], [10, 10, 3])
        self.assertEqual(len(result), 23)
        self.assertAlmostEqual(float(np.linalg.norm(result[0])), 1)

    async def test_provider_auth_failure_never_echoes_key_or_fake_vectors(self):
        provider = BailianEmbedding(Settings(api_key="private-test-key"),
            httpx.MockTransport(lambda request: httpx.Response(401, json={"error": {"message": "private-test-key"}})))
        with self.assertRaises(EmbeddingError) as raised:
            await provider.embed(["频谱"])
        self.assertNotIn("private-test-key", str(raised.exception))

    def test_keys_are_not_returned_and_endpoint_cannot_send_them_elsewhere(self):
        config = SettingsStore(self.temporary.name)
        settings = config.save({"api_key": "private-test-key"})
        self.assertNotIn("private-test-key", json.dumps(settings.public()))
        with self.assertRaises(ValueError):
            config.save({"base_url": "http://127.0.0.1:8080"})
        with self.assertRaises(ValueError):
            config.save({"base_url": "https://attacker.example/compatible-mode/v1"})

    def test_invalid_vectors_are_rejected(self):
        for values in ([0] * 256, [float("nan")] * 256, [1] * 3):
            with self.assertRaises(EmbeddingError):
                normalized_vector(values, 256)

    def test_invalid_or_scanned_pdf_is_rejected_without_indexing(self):
        from pypdf import PdfWriter
        with self.assertRaisesRegex(ValueError, "PDF 格式损坏"):
            extract_pages("损坏.pdf", b"not a PDF")
        buffer = io.BytesIO()
        writer = PdfWriter()
        writer.add_blank_page(width=300, height=300)
        writer.write(buffer)
        with self.assertRaisesRegex(ValueError, "OCR"):
            extract_pages("扫描.pdf", buffer.getvalue())


class ApiBoundaryTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        # Import into a separate temporary database, never the running /data volume.
        with patch.dict(os.environ, {"RAGFLOW_DATA_DIR": cls.temporary.name, "RAGFLOW_MCP_TOKEN": "test-mcp-token"}):
            cls.server = importlib.import_module("retrieval_lab.server")
            cls.application = cls.server.LocalBoundary(cls.server.mcp.streamable_http_app())

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    async def asyncSetUp(self):
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.application),
            base_url="http://127.0.0.1:18004", headers={"Authorization": "Bearer test-mcp-token"})

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_host_origin_and_mcp_auth_boundaries(self):
        self.assertEqual((await self.client.get("/mcp", headers={"Authorization": ""})).status_code, 401)
        self.assertEqual((await self.client.get("/api/overview", headers={"Host": "attacker.example"})).status_code, 421)
        self.assertEqual((await self.client.post("/api/settings", json={}, headers={"Origin": "https://attacker.example"})).status_code, 403)
        self.assertEqual((await self.client.post("/api/settings", json={}, headers={"Authorization": ""})).status_code, 403)
        self.assertEqual((await self.client.post("/api/settings", json={},
            headers={"Authorization": "", "Origin": "http://127.0.0.1:18004"})).status_code, 200)

    async def test_public_settings_hide_key_and_invalid_save_preserves_profile(self):
        response = await self.client.post("/api/settings", json={"api_key": "private-api-test-key"})
        self.assertEqual(response.status_code, 200)
        response = await self.client.get("/api/overview")
        self.assertNotIn("private-api-test-key", response.text)
        previous = response.json()["embedding"]
        response = await self.client.post("/api/settings", json={"base_url": "https://attacker.example"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual((await self.client.get("/api/overview")).json()["embedding"], previous)

    async def test_upload_failure_preserves_original_and_delete_requires_boolean_confirmation(self):
        response = await self.client.post("/api/documents", files={"file": ("失败重试.md", "能量检测测试".encode())})
        self.assertEqual(response.status_code, 200)
        identifier = response.json()["id"]
        with patch.object(self.server, "BailianEmbedding", return_value=FakeEmbedding(fail=True)):
            response = await self.client.post(f"/api/documents/{identifier}/index")
        self.assertEqual(response.status_code, 503)
        overview = (await self.client.get("/api/overview")).json()
        document = next(d for d in overview["documents"] if d["id"] == identifier)
        self.assertEqual(document["status"], "failed")
        self.assertEqual(overview["indexed_chunk_count"], 0)
        original = (await self.client.get(f"/api/documents/{identifier}/chunks")).json()
        self.assertIn("能量检测测试", original["chunks"][0]["text"])
        for confirmation in (None, False, "true", 1):
            response = await self.client.post(f"/api/documents/{identifier}/delete", json={"confirmed": confirmation})
            self.assertEqual(response.status_code, 400)
        self.assertEqual((await self.client.post(f"/api/documents/{identifier}/delete", json={"confirmed": True})).status_code, 200)


if __name__ == "__main__":
    unittest.main()
