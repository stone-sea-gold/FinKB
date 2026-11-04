"""向量化（search/embed.py）单测，全部离线。"""

from __future__ import annotations

import numpy as np
import pytest

from finkb.search import embed


def test_fake_embedder_is_deterministic_and_normalized():
    embedder = embed.FakeEmbedder(dims=32)
    first = embedder.embed(["营业收入同比增长"])[0]
    second = embedder.embed(["营业收入同比增长"])[0]
    assert first == second
    assert len(first) == 32
    assert abs(float(np.linalg.norm(first)) - 1.0) < 1e-5


def test_fake_embedder_different_texts_differ():
    embedder = embed.FakeEmbedder()
    a, b = embedder.embed(["存货增加", "研发投入"])
    assert a != b


def test_load_env_reads_file(tmp_path, monkeypatch):
    monkeypatch.setattr(embed, "load_env", embed.load_env)
    path = tmp_path / ".env"
    path.write_text("SILICONFLOW_API_KEY=sk-test\n# comment\nOTHER=1\n", encoding="utf-8")
    values = embed.load_env(path)
    assert values["SILICONFLOW_API_KEY"] == "sk-test"


def test_missing_key_raises(monkeypatch):
    monkeypatch.setattr(embed, "load_env", lambda path=None: {})
    with pytest.raises(embed.EmbedderError):
        embed.SiliconFlowEmbedder()


def test_batches_split(monkeypatch):
    monkeypatch.setattr(embed, "load_env", lambda path=None: {"SILICONFLOW_API_KEY": "sk-test"})
    embedder = embed.SiliconFlowEmbedder(batch_size=2)
    calls: list[int] = []

    def fake_post(batch):
        calls.append(len(batch))
        return [[0.0] for _ in batch]

    monkeypatch.setattr(embedder, "_post", fake_post)
    vectors = embedder.embed(["a", "b", "c", "d", "e"])
    assert calls == [2, 2, 1]
    assert len(vectors) == 5
