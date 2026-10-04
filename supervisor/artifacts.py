"""Deliver verified HTML outputs through OWUI's file store and sandboxed preview."""

import asyncio
import base64
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import uuid
from xml.etree import ElementTree


MAX_HTML_BYTES = 1024 * 1024


def html_path(path, directory):
    root = PurePosixPath(directory)
    candidate = PurePosixPath(path)
    if not candidate.is_absolute():
        candidate = root / candidate
    if ".." in candidate.parts or not candidate.is_relative_to(root):
        return None
    return str(candidate) if candidate.suffix.lower() in {".html", ".htm", ".svg"} else None


def svg_preview(content):
    if len(content.encode("utf-8")) > MAX_HTML_BYTES:
        raise ValueError("SVG 超过预览大小限制。")
    if re.search(r"<!DOCTYPE|<!ENTITY", content, re.I):
        raise ValueError("SVG 不能包含外部文档或实体声明。")
    root = ElementTree.fromstring(content)
    if root.tag not in {"svg", "{http://www.w3.org/2000/svg}svg"}:
        raise ValueError("文件没有有效的 SVG 根元素。")
    encoded = base64.b64encode(content.encode("utf-8")).decode()
    return ('<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<style>html,body{margin:0;width:100%;height:100%;background:white}body{display:grid;place-items:center}'
            'img{max-width:100%;max-height:100%;object-fit:contain}</style></head><body>'
            '<img alt="SVG illustration" src="data:image/svg+xml;base64,' + encoded + '"></body></html>')


def html_markdown(name, url, content):
    # A generated script can contain backticks; do not let it end the code fence.
    longest = max((len(match.group()) for match in re.finditer(r"`+", content)), default=0)
    fence = "`" * max(3, longest + 1)
    label = name.replace("[", "").replace("]", "").replace("\n", " ")
    return (f"\n\n📎 [{label}]({url})\n\n"
            f"点击 HTML 代码块的预览按钮即可观看，下载后也可用浏览器打开。\n\n"
            f"{fence}html\n{content}\n{fence}\n")


async def save_html(user_id, name, content, content_type="text/html"):
    from open_webui.models.files import FileForm, Files
    from open_webui.storage.provider import Storage

    data = content.encode("utf-8")
    if not data or len(data) > MAX_HTML_BYTES:
        raise ValueError("HTML 超过预览大小限制，或文件为空。")
    if content_type == "image/svg+xml":
        svg_preview(content)
    file_id = str(uuid.uuid4())
    _, storage_path = await asyncio.to_thread(Storage.upload_file, io.BytesIO(data),
        file_id + "_" + name, {"OpenWebUI-User-Id": user_id, "OpenWebUI-File-Id": file_id})
    inserted = await Files.insert_new_file(user_id, FileForm(id=file_id, filename=name,
        path=storage_path, data={}, meta={"name": name, "content_type": content_type, "size": len(data)}))
    if inserted is None:
        raise RuntimeError("HTML 附件保存失败。")
    return {"file_id": file_id, "name": name, "url": f"/api/v1/files/{file_id}/content"}


async def save_screenshot(user_id, image):
    from open_webui.models.files import FileForm, Files
    from open_webui.storage.provider import Storage

    data = base64.b64decode(image["data"], validate=True)
    if len(data) > 4 * 1024 * 1024 or not data.startswith(b"\x89PNG\r\n\x1a\n") or hashlib.sha256(data).hexdigest() != image["sha256"]:
        raise ValueError("截图校验失败。")
    file_id, name = str(uuid.uuid4()), PurePosixPath(image["name"]).name
    _, path = await asyncio.to_thread(Storage.upload_file, io.BytesIO(data), file_id + "_" + name,
        {"OpenWebUI-User-Id": user_id, "OpenWebUI-File-Id": file_id})
    inserted = await Files.insert_new_file(user_id, FileForm(id=file_id, filename=name, path=path,
        data={}, meta={"name": name, "content_type": "image/png", "size": len(data)}))
    if inserted is None:
        raise RuntimeError("验收截图保存失败。")
    return {"file_id": file_id, "name": name, "url": f"/api/v1/files/{file_id}/content", "sha256": image["sha256"]}


async def deliver_html(report, tools, user_id, directory, unwrap):
    artifacts, errors, seen = [], [], set()
    if report.get("status") != "completed":
        return artifacts, errors
    verification = report.get("verification") or {}
    candidates = list(report.get("changed_files") or [])
    if verification.get("passed") is True:
        candidates.extend(item.get("target") for item in verification.get("browser", [])
                          if item.get("passed") is True and item.get("source_sha256"))
    for raw in candidates:
        if not isinstance(raw, str):
            continue
        path = html_path(raw, directory)
        if not path:
            if PurePosixPath(raw).suffix.lower() in {".html", ".htm", ".svg"}:
                errors.append({"path": raw, "error": "HTML/SVG 未保存在当前项目目录，用户的文件栏无法显示。"})
            continue
        if path in seen:
            continue
        seen.add(path)
        try:
            payload = unwrap(await tools["read_file"]["callable"](path=path))
            if not isinstance(payload, dict) or not isinstance(payload.get("content"), str):
                raise ValueError("工具未返回可读取的 HTML 内容。")
            content = payload["content"]
            if payload.get("total_lines", 0) > len(content.splitlines()):
                raise ValueError("HTML 读取不完整，无法提供预览。")
            expected = next((item.get("source_sha256") for item in verification.get("browser", [])
                             if html_path(item.get("target", ""), directory) == path), None)
            if verification.get("passed") is True and (not expected or hashlib.sha256(content.encode("utf-8")).hexdigest() != expected):
                raise ValueError("交付文件与已验收文件不一致，不能提供未经核对的附件。")
            if PurePosixPath(path).suffix.lower() == ".svg":
                artifact = await save_html(user_id, PurePosixPath(path).name, content, content_type="image/svg+xml")
            else:
                artifact = await save_html(user_id, PurePosixPath(path).name, content)
            artifacts.append({**artifact, "path": path})
        except Exception as exc:
            errors.append({"path": path, "error": str(exc)})
    return artifacts, errors


def current_artifacts(messages):
    calls, artifacts = set(), []
    for message in messages:
        if message.get("role") == "user":
            calls, artifacts = set(), []
        elif message.get("role") == "assistant":
            for call in message.get("tool_calls") or []:
                if call.get("function", {}).get("name", "").endswith("delegate_coding_task"):
                    calls.add(call.get("id"))
        elif message.get("role") == "tool" and message.get("tool_call_id") in calls:
            try:
                report = json.loads(message.get("content") or "{}")
            except (ValueError, TypeError):
                continue
            if isinstance(report, dict) and report.get("status") == "completed":
                artifacts.extend(item for item in report.get("artifacts", [])
                                 if isinstance(item, dict) and item.get("file_id"))
                if (report.get("verification") or {}).get("passed") is True:
                    artifacts.extend(item for item in report["verification"].get("screenshots", [])
                                     if isinstance(item, dict) and item.get("file_id"))
    return artifacts


async def previews(messages, user_id):
    artifacts = current_artifacts(messages)
    if not user_id or not artifacts:
        return ""
    from open_webui.models.files import Files
    from open_webui.storage.provider import Storage

    chunks, seen = [], set()
    for artifact in artifacts:
        file_id = artifact["file_id"]
        if file_id in seen:
            continue
        seen.add(file_id)
        file = await Files.get_file_by_id(file_id)
        # Tool results cannot select another user's stored file or forge a preview URL.
        if file is None or file.user_id != user_id or file.meta.get("content_type") not in {"text/html", "image/png", "image/svg+xml"}:
            continue
        if file.meta.get("content_type") == "image/png":
            if file.meta.get("size", 4 * MAX_HTML_BYTES + 1) <= 4 * MAX_HTML_BYTES:
                label = file.filename.replace("[", "").replace("]", "").replace("\n", " ")
                chunks.append(f"\n\n[验收截图：{label}](/api/v1/files/{file_id}/content)")
            continue
        if file.meta.get("size", MAX_HTML_BYTES + 1) > MAX_HTML_BYTES:
            continue
        path = await asyncio.to_thread(Storage.get_file, file.path)
        data = await asyncio.to_thread(Path(path).read_bytes)
        if len(data) > MAX_HTML_BYTES:
            continue
        content = data.decode("utf-8")
        if file.meta.get("content_type") == "image/svg+xml":
            try:
                content = svg_preview(content)
            except (ValueError, ElementTree.ParseError):
                continue
        chunks.append(html_markdown(file.filename, f"/api/v1/files/{file_id}/content", content))
    return "".join(chunks)
