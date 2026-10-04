from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import urlparse

import httpx
import numpy as np


class EmbeddingError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    model: str = "text-embedding-v4"
    dimensions: int = 1024
    api_key: str = ""

    @property
    def profile(self):
        return hashlib.sha256(f"{self.base_url}|{self.model}|{self.dimensions}".encode()).hexdigest()

    def public(self):
        return {"base_url": self.base_url, "model": self.model, "dimensions": self.dimensions,
                "key_configured": bool(self.api_key), "provider": "阿里云百炼"}


def validate_settings(settings):
    url = urlparse(settings.base_url)
    if (url.scheme != "https" or not url.hostname or not url.hostname.endswith(".aliyuncs.com")
            or url.username or url.password or url.query or url.fragment or url.port not in (None, 443)):
        raise ValueError("请填写百炼提供的 HTTPS 兼容 API 地址（aliyuncs.com 域名）。")
    if settings.model not in {"text-embedding-v4", "text-embedding-v3", "qwen3.7-text-embedding", "qwen3.7-text-embedding-flash"}:
        raise ValueError("请选择受支持的百炼文本向量模型。")
    if settings.dimensions not in {256, 512, 768, 1024}:
        raise ValueError("实验平台支持 256、512、768 或 1024 维。")


class SettingsStore:
    def __init__(self, directory):
        self.path = Path(directory) / "bailian-settings.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self):
        if self.path.exists():
            saved = json.loads(self.path.read_text())
            return Settings(**saved)
        return Settings(base_url=os.environ.get("DASHSCOPE_BASE_URL") or Settings.base_url,
                        model=os.environ.get("DASHSCOPE_EMBEDDING_MODEL") or Settings.model,
                        api_key=os.environ.get("DASHSCOPE_API_KEY", ""))

    def save(self, data):
        old = self.load()
        settings = Settings(base_url=str(data.get("base_url") or old.base_url).rstrip("/"),
            model=str(data.get("model") or old.model), dimensions=int(data.get("dimensions", old.dimensions)),
            api_key=str(data.get("api_key") or old.api_key).strip())
        validate_settings(settings)
        temporary = self.path.with_suffix(".tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w") as output:
            json.dump(settings.__dict__, output)
        temporary.replace(self.path)
        self.path.chmod(0o600)
        return settings


def normalized_vector(values, dimensions):
    vector = np.asarray(values, dtype=np.float32)
    if vector.shape != (dimensions,) or not np.isfinite(vector).all():
        raise EmbeddingError("百炼返回了无效或维度不匹配的向量。")
    norm = float(np.linalg.norm(vector))
    if norm <= 0:
        raise EmbeddingError("百炼返回了零向量。")
    return vector / norm


class BailianEmbedding:
    def __init__(self, settings, transport=None):
        self.settings, self.transport = settings, transport

    async def embed(self, texts):
        validate_settings(self.settings)
        if not self.settings.api_key:
            raise EmbeddingError("请先在百炼配置中填写有效 API Key。")
        vectors = []
        async with httpx.AsyncClient(timeout=httpx.Timeout(45, connect=10),
                                     transport=self.transport, follow_redirects=False) as client:
            for offset in range(0, len(texts), 10):
                batch = texts[offset:offset + 10]
                try:
                    response = await client.post(self.settings.base_url + "/embeddings",
                        headers={"Authorization": "Bearer " + self.settings.api_key},
                        json={"model": self.settings.model, "input": batch,
                              "dimensions": self.settings.dimensions, "encoding_format": "float"})
                except httpx.HTTPError:
                    raise EmbeddingError("无法连接百炼 embedding 接口，请检查地址和网络。") from None
                if response.status_code in {401, 403}:
                    raise EmbeddingError(f"百炼鉴权失败（HTTP {response.status_code}），请检查 Key、地域和业务空间地址。")
                if response.status_code == 429:
                    raise EmbeddingError("百炼请求限流，请稍后重试。")
                if response.status_code != 200:
                    raise EmbeddingError(f"百炼 embedding 请求失败（HTTP {response.status_code}）。")
                try:
                    rows = response.json()["data"]
                    if len(rows) != len(batch) or sorted(r["index"] for r in rows) != list(range(len(batch))):
                        raise ValueError()
                    for row in sorted(rows, key=lambda item: item["index"]):
                        vectors.append(normalized_vector(row["embedding"], self.settings.dimensions))
                except (KeyError, TypeError, ValueError):
                    raise EmbeddingError("百炼 embedding 响应格式不正确。") from None
        return vectors
