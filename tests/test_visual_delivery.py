import json
import base64
import hashlib
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from supervisor.artifacts import current_artifacts, deliver_html, html_markdown, previews, svg_preview


def history(artifacts, status="completed"):
    return [{"role": "user", "content": "生成动画"},
            {"role": "assistant", "tool_calls": [{"id": "coding-1", "function": {
                "name": "coding_agent_delegate_coding_task"}}]},
            {"role": "tool", "tool_call_id": "coding-1", "content": json.dumps({
                "status": status, "artifacts": artifacts})}]


class VisualDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_verified_complete_workspace_html_is_uploaded(self):
        html = "<!DOCTYPE html>\n<html><svg/></html>"
        read = AsyncMock(return_value={"content": html, "total_lines": 2})
        save = AsyncMock(return_value={"file_id": "verified", "name": "scene.html", "url": "/real"})
        report = {"status": "completed", "changed_files": [
            "/home/user/scene.html", "../other.html", "scene.html", "scene.html", "tests.py"]}
        with patch("supervisor.artifacts.save_html", save):
            artifacts, errors = await deliver_html(report, {"read_file": {"callable": read}},
                                                   "owner", "/workspace/owner", lambda value: value)
        read.assert_awaited_once_with(path="/workspace/owner/scene.html")
        save.assert_awaited_once_with("owner", "scene.html", html)
        self.assertEqual(artifacts[0]["path"], "/workspace/owner/scene.html")
        self.assertEqual(len(errors), 2)
        self.assertTrue(all("用户的文件栏" in error["error"] for error in errors))

    async def test_failed_task_or_truncated_read_does_not_claim_delivery(self):
        read = AsyncMock(return_value={"content": "<html>", "total_lines": 100})
        save = AsyncMock()
        with patch("supervisor.artifacts.save_html", save):
            artifacts, errors = await deliver_html({"status": "failed", "changed_files": ["a.html"]},
                {"read_file": {"callable": read}}, "owner", "/workspace/owner", lambda value: value)
            read.assert_not_awaited()
            artifacts, errors = await deliver_html({"status": "completed", "changed_files": ["a.html"]},
                {"read_file": {"callable": read}}, "owner", "/workspace/owner", lambda value: value)
        save.assert_not_awaited()
        self.assertEqual(artifacts, [])
        self.assertIn("读取不完整", errors[0]["error"])

    def test_previous_turn_failed_or_unrelated_tool_cannot_supply_artifacts(self):
        artifacts = [{"file_id": "file-1"}]
        self.assertEqual(current_artifacts(history(artifacts)), artifacts)
        self.assertFalse(current_artifacts(history(artifacts, "failed")))
        self.assertFalse(current_artifacts(history(artifacts) + [{"role": "user", "content": "新问题"}]))
        messages = history(artifacts)
        messages[-1]["tool_call_id"] = "another-tool"
        self.assertFalse(current_artifacts(messages))

    async def test_preview_checks_file_owner_and_uses_stored_content_and_url(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "scene.html"
            html = '<html><script>const text = "```";</script></html>'
            path.write_text(html, encoding="utf-8")
            file = SimpleNamespace(user_id="other-user", filename="scene.html", path=str(path),
                                   meta={"content_type": "text/html", "size": len(html)})
            files = SimpleNamespace(get_file_by_id=AsyncMock(return_value=file))
            storage = SimpleNamespace(get_file=Mock(return_value=str(path)))
            modules = {"open_webui.models.files": SimpleNamespace(Files=files),
                       "open_webui.storage.provider": SimpleNamespace(Storage=storage)}
            messages = history([{"file_id": "file-1", "url": "https://forged.invalid"}])
            with patch.dict("sys.modules", modules):
                self.assertEqual(await previews(messages, "owner"), "")
                storage.get_file.assert_not_called()
                file.user_id = "owner"
                markdown = await previews(messages, "owner")
            self.assertIn("/api/v1/files/file-1/content", markdown)
            self.assertNotIn("forged.invalid", markdown)
            self.assertIn("````html\n" + html + "\n````", markdown)

    async def test_svg_original_and_image_mode_preview_use_verified_file_bytes(self):
        svg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 400"><circle cx="200" cy="200" r="80"/></svg>'
        digest = hashlib.sha256(svg.encode()).hexdigest()
        read = AsyncMock(return_value={'content': svg, 'total_lines': 1})
        save = AsyncMock(return_value={'file_id': 'svg-file', 'name': 'cat.svg'})
        report = {'status': 'completed', 'changed_files': ['cat.svg'], 'verification': {
            'passed': True, 'browser': [{'target': 'cat.svg', 'source_sha256': digest}]}}
        with patch('supervisor.artifacts.save_html', save):
            artifacts, errors = await deliver_html(report, {'read_file': {'callable': read}},
                                                   'owner', '/workspace/owner', lambda value: value)
        self.assertFalse(errors)
        save.assert_awaited_once_with('owner', 'cat.svg', svg, content_type='image/svg+xml')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'cat.svg'
            path.write_text(svg, encoding='utf-8')
            stored = SimpleNamespace(user_id='owner', filename='cat.svg', path=str(path),
                                     meta={'content_type': 'image/svg+xml', 'size': len(svg)})
            modules = {'open_webui.models.files': SimpleNamespace(Files=SimpleNamespace(get_file_by_id=AsyncMock(return_value=stored))),
                'open_webui.storage.provider': SimpleNamespace(Storage=SimpleNamespace(get_file=Mock(return_value=str(path))))}
            with patch.dict('sys.modules', modules):
                markup = await previews(history(artifacts), 'owner')
        self.assertIn('/api/v1/files/svg-file/content', markup)
        self.assertIn('data:image/svg+xml;base64,' + base64.b64encode(svg.encode()).decode(), markup)
        self.assertNotIn('<circle', markup)

    async def test_svg_changed_after_acceptance_is_not_delivered(self):
        read = AsyncMock(return_value={'content': '<svg/>', 'total_lines': 1})
        save = AsyncMock()
        report = {'status': 'completed', 'changed_files': ['cat.svg'], 'verification': {
            'passed': True, 'browser': [{'target': 'cat.svg', 'source_sha256': 'different'}]}}
        with patch('supervisor.artifacts.save_html', save):
            artifacts, errors = await deliver_html(report, {'read_file': {'callable': read}},
                                                   'owner', '/workspace/owner', lambda value: value)
        save.assert_not_awaited()
        self.assertFalse(artifacts)
        self.assertIn('与已验收文件不一致', errors[0]['error'])

    def test_svg_preview_rejects_invalid_roots_and_entities(self):
        for content in ('<html/>', '<!DOCTYPE svg [<!ENTITY content "unsafe">]><svg/>', '<svg>'):
            with self.assertRaises(Exception):
                svg_preview(content)

    async def test_existing_unchanged_svg_with_successful_acceptance_still_has_attachment(self):
        svg = '<svg xmlns="http://www.w3.org/2000/svg"><circle r="20"/></svg>'
        read = AsyncMock(return_value={'content': svg, 'total_lines': 1})
        save = AsyncMock(return_value={'file_id': 'svg-file', 'name': 'cat.svg'})
        report = {'status': 'completed', 'changed_files': [], 'verification': {'passed': True, 'browser': [
            {'target': 'cat.svg', 'passed': True, 'source_sha256': hashlib.sha256(svg.encode()).hexdigest()}]}}
        with patch('supervisor.artifacts.save_html', save):
            artifacts, errors = await deliver_html(report, {'read_file': {'callable': read}}, 'owner',
                                                   '/workspace/owner', lambda value: value)
        self.assertFalse(errors)
        self.assertEqual(artifacts[0]['file_id'], 'svg-file')


if __name__ == "__main__":
    unittest.main()
