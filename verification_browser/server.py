"""Render only the authorized terminal workspace, in an isolated browser."""

import asyncio
import base64
import hashlib
import hmac
import io
import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from fastapi import FastAPI, Header, HTTPException
from PIL import Image, ImageStat
from playwright.async_api import async_playwright
from pydantic import BaseModel


app = FastAPI()
ROOT = Path(os.environ.get("VERIFICATION_WORKSPACE", "/workspace/wenguang.wu")).resolve()
render_lock = asyncio.Lock()


class RenderRequest(BaseModel):
    directory: str
    target: str


def authorized_target(directory, target):
    project = Path(directory).resolve()
    if not project.is_relative_to(ROOT) or not project.is_dir():
        raise ValueError("预览目录不在授权工作区内。")
    parts = urlsplit(target)
    if parts.scheme:
        if parts.scheme != "http" or parts.hostname not in {"localhost", "127.0.0.1", "open-terminal"}:
            raise ValueError("应用预览只支持当前 OpenTerminal 内的本地 HTTP 服务。")
        if parts.username or parts.password or not parts.port or parts.port == 8000:
            raise ValueError("请提供应用服务器端口，不能使用终端管理接口。")
        return urlunsplit(("http", f"open-terminal:{parts.port}", parts.path or "/", parts.query, "")), None
    path = (project / target).resolve()
    if not path.is_relative_to(project) or path.suffix.lower() not in {".html", ".htm", ".svg"} or not path.is_file():
        raise ValueError("预览文件必须是项目内真实存在的 HTML 或 SVG。")
    return path.as_uri(), path


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/render")
async def render(request: RenderRequest, authorization: str = Header(default="")):
    expected = "Bearer " + os.environ.get("OPEN_TERMINAL_API_KEY", "")
    if expected == "Bearer " or not hmac.compare_digest(authorization, expected):
        raise HTTPException(401, "Unauthorized")
    try:
        url, path = authorized_target(request.directory, request.target)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    svg_data = path.read_bytes() if path and path.suffix.lower() == ".svg" else None
    async with render_lock, asyncio.timeout(60), async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            screenshots, issues, measurements = [], [], []
            for name, width, height in (("desktop", 1280, 800), ("mobile", 390, 844)):
                context = await browser.new_context(viewport={"width": width, "height": height}, device_scale_factor=1)
                page = await context.new_page()
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
                page.on("requestfailed", lambda item: errors.append("资源加载失败：" + item.url))
                page.on("response", lambda item: errors.append(f"HTTP {item.status}: {item.url}") if item.status >= 400 else None)
                if svg_data is not None:
                    # A drawing's nominal dimensions must not make it overflow a phone.
                    # Image mode also keeps SVG scripts out of the preview document.
                    await page.set_content('<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">'
                        '<style>html,body{margin:0;width:100%;height:100%;background:white}body{display:grid;place-items:center}'
                        'img{max-width:100%;max-height:100%;object-fit:contain}</style></head><body>'
                        '<img alt="SVG illustration" src="data:image/svg+xml;base64,'
                        + base64.b64encode(svg_data).decode() + '"></body></html>', wait_until="load")
                else:
                    response = await page.goto(url, wait_until="load", timeout=20000)
                    if response and response.status >= 400:
                        errors.append(f"页面 HTTP {response.status}")
                await page.wait_for_timeout(500)
                metrics = await page.evaluate("""() => {
                    const root=document.body || document.documentElement;
                    return {
                    width: innerWidth, scrollWidth: document.documentElement.scrollWidth,
                    visible: [...root.querySelectorAll('*')].some(e => {
                        const r=e.getBoundingClientRect(), s=getComputedStyle(e);
                        return r.width>1 && r.height>1 && s.visibility!=='hidden' && s.display!=='none';
                    }) || (root.textContent || '').trim().length>0
                }}""")
                if not metrics["visible"]:
                    errors.append("页面没有可见内容。")
                if metrics["scrollWidth"] > width + 3:
                    errors.append("页面发生横向溢出。")
                measurements.append({"viewport": name, **metrics})
                # Two desktop frames let the review catch detached animated parts.
                for frame in range(2 if name == "desktop" else 1):
                    if frame:
                        await page.wait_for_timeout(900)
                    data = await page.screenshot(full_page=False, type="png", timeout=10000)
                    picture = Image.open(io.BytesIO(data)).convert("RGB")
                    if max(ImageStat.Stat(picture).stddev) < 0.3:
                        errors.append("截图为空白或纯色。")
                    screenshots.append({"name": f"{name}-{frame + 1}.png", "mime_type": "image/png",
                        "sha256": hashlib.sha256(data).hexdigest(), "data": base64.b64encode(data).decode()})
                issues.extend({"viewport": name, "error": error[:1000]} for error in sorted(set(errors)))
                await context.close()
            return {"passed": not issues, "issues": issues, "measurements": measurements,
                    "source_sha256": hashlib.sha256(svg_data if svg_data is not None else path.read_bytes()).hexdigest() if path else None,
                    "screenshots": screenshots}
        finally:
            await browser.close()
