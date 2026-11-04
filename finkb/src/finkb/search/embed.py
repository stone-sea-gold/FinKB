"""向量化（S3）：``Embedder`` 协议 + SiliconFlow bge-m3 + 离线 Fake。

设计约束：pytest 不触网，所以测试用 ``FakeEmbedder``（确定性哈希向量）；
真实索引走硅基流动 ``BAAI/bge-m3``（1024 维，key 在 ``config/.env``）。
没有 key 或网络失败时由 ``hybrid.search`` 自动降级为 FTS-only，不影响问答可用性。
"""

from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path
from typing import Protocol

import httpx
import numpy as np

from finkb.fact_engine.selector import FINKB_ROOT

DEFAULT_MODEL = "BAAI/bge-m3"
DEFAULT_BASE_URL = "https://api.siliconflow.cn/v1"
BATCH_SIZE = 64


class EmbedderError(RuntimeError):
    """向量化失败（缺 key、限流、网络错误）。"""


def load_env(path: str | Path | None = None) -> dict[str, str]:
    """读 ``config/.env``；已存在的环境变量优先。"""
    file = Path(path or (FINKB_ROOT / "config" / ".env"))
    values: dict[str, str] = {}
    if file.exists():
        for line in file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    values.update({k: v for k, v in os.environ.items() if k in values or k.startswith("SILICONFLOW_")})
    return values


class Embedder(Protocol):
    dims: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class SiliconFlowEmbedder:
    """硅基流动 OpenAI 兼容 embeddings 端点。"""

    def __init__(
        self,
        api_key: str | None = None,
        model: str = DEFAULT_MODEL,
        base_url: str | None = None,
        batch_size: int = BATCH_SIZE,
        timeout: float = 120.0,
        retries: int = 6,
    ) -> None:
        env = load_env()
        self.api_key = api_key or env.get("SILICONFLOW_API_KEY", "")
        if not self.api_key:
            raise EmbedderError("缺少 SILICONFLOW_API_KEY（config/.env）")
        self.model = model
        self.base_url = (base_url or env.get("SILICONFLOW_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.batch_size = batch_size
        self.timeout = timeout
        self.retries = retries
        self.dims = 1024

    def _post(self, batch: list[str]) -> list[list[float]]:
        last_error = ""
        for attempt in range(self.retries):
            try:
                response = httpx.post(
                    f"{self.base_url}/embeddings",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json={"model": self.model, "input": batch},
                    timeout=self.timeout,
                )
            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            else:
                if response.status_code == 200:
                    data = response.json()["data"]
                    return [item["embedding"] for item in sorted(data, key=lambda item: item["index"])]
                last_error = f"HTTP {response.status_code}: {response.text[:200]}"
                if response.status_code not in (429, 500, 502, 503, 504):
                    break
            time.sleep(min(2**attempt, 30))
        raise EmbedderError(f"embedding 失败：{last_error}")

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self.batch_size):
            vectors.extend(self._post(texts[start : start + self.batch_size]))
        return vectors


class FakeEmbedder:
    """确定性哈希向量，供离线测试；同一文本永远同一向量。"""

    def __init__(self, dims: int = 64) -> None:
        self.dims = dims

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            seed = int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "big")
            vector = np.random.default_rng(seed).normal(size=self.dims).astype(np.float32)
            norm = float(np.linalg.norm(vector)) or 1.0
            vectors.append((vector / norm).tolist())
        return vectors


def main() -> None:
    import argparse
    import json

    parser = argparse.ArgumentParser(description="向量化自检")
    parser.add_argument("--text", default="营业收入同比增长")
    args = parser.parse_args()
    embedder = SiliconFlowEmbedder()
    vectors = embedder.embed([args.text])
    print(json.dumps({"model": embedder.model, "dims": len(vectors[0]), "head": vectors[0][:3]}))


if __name__ == "__main__":
    main()
