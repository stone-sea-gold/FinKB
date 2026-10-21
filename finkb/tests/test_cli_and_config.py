"""配置加载与 CLI 的验收测试。

覆盖三件事：
1. ``.env`` 解析与注入（不覆盖已有环境变量）
2. ``MineruConfig.from_env`` 的取值与缺 token 时的快速失败
3. CLI 离线端到端：能产出 page_anchored.md / report.json 且不触网
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from finkb.cli import apply_env_file, load_env_file, main
from finkb.parser.mineru_client import MineruClient, MineruConfig, MineruError

DOC = "002594_2025_年报"


# --------------------------------------------------------------------------- #
# .env 解析
# --------------------------------------------------------------------------- #
def test_load_env_file_parses_basic_pairs(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(
        "# 注释行\n"
        "MINERU_API_TOKEN=abc123\n"
        "\n"
        "export MINERU_LANGUAGE=ch\n"
        'QUOTED="hello world"\n'
        "SINGLE='single'\n",
        encoding="utf-8",
    )

    values = load_env_file(env)

    assert values["MINERU_API_TOKEN"] == "abc123"
    assert values["MINERU_LANGUAGE"] == "ch"
    assert values["QUOTED"] == "hello world"
    assert values["SINGLE"] == "single"


def test_load_env_file_missing_returns_empty(tmp_path: Path) -> None:
    assert load_env_file(tmp_path / "不存在.env") == {}


def test_apply_env_file_does_not_override_existing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = tmp_path / ".env"
    env.write_text("MINERU_API_TOKEN=from_file\n", encoding="utf-8")
    monkeypatch.setenv("MINERU_API_TOKEN", "from_shell")

    apply_env_file(env)

    assert os.environ["MINERU_API_TOKEN"] == "from_shell"


# --------------------------------------------------------------------------- #
# MineruConfig.from_env
# --------------------------------------------------------------------------- #
def test_from_env_reads_token_and_defaults() -> None:
    config = MineruConfig.from_env({"MINERU_API_TOKEN": "tok-1"})

    assert config.token == "tok-1"
    assert config.language == "ch"
    assert config.model_version == "pipeline"


def test_from_env_accepts_optional_overrides() -> None:
    config = MineruConfig.from_env(
        {
            "MINERU_API_TOKEN": "tok-1",
            "MINERU_MODEL_VERSION": "vlm",
            "MINERU_LANGUAGE": "en",
            "MINERU_POLL_INTERVAL": "0.5",
            "MINERU_POLL_TIMEOUT": "60",
        }
    )

    assert config.model_version == "vlm"
    assert config.language == "en"
    assert config.poll_interval == 0.5
    assert config.poll_timeout == 60.0


def test_from_env_raises_without_token() -> None:
    with pytest.raises(MineruError, match="MINERU_API_TOKEN"):
        MineruConfig.from_env({})


def test_from_env_ignores_blank_token() -> None:
    with pytest.raises(MineruError):
        MineruConfig.from_env({"MINERU_API_TOKEN": "   "})


def test_from_env_tolerates_bad_number() -> None:
    config = MineruConfig.from_env(
        {"MINERU_API_TOKEN": "t", "MINERU_POLL_INTERVAL": "abc"}
    )
    assert config.poll_interval == 3.0  # 回退默认值，不抛异常


# --------------------------------------------------------------------------- #
# page_ranges 透传到请求体
# --------------------------------------------------------------------------- #
def test_request_upload_urls_includes_page_ranges() -> None:
    captured: dict = {}

    class Session:
        def request(self, method, url, **kwargs):
            captured.update(kwargs.get("json") or {})

            class Resp:
                status_code = 200
                text = ""

                def json(self):
                    return {
                        "code": 0,
                        "data": {"batch_id": "b", "file_urls": ["https://u"]},
                    }

            return Resp()

    client = MineruClient(MineruConfig(token="t"), session=Session())
    batch_id, _ = client.request_upload_urls(
        "a.pdf", data_id=DOC, page_ranges="43-44"
    )

    assert batch_id == "b"
    assert captured["files"][0]["page_ranges"] == "43-44"
    assert captured["files"][0]["data_id"] == DOC


def test_request_upload_urls_omits_page_ranges_when_absent() -> None:
    captured: dict = {}

    class Session:
        def request(self, method, url, **kwargs):
            captured.update(kwargs.get("json") or {})

            class Resp:
                status_code = 200
                text = ""

                def json(self):
                    return {"code": 0, "data": {"batch_id": "b", "file_urls": ["u"]}}

            return Resp()

    MineruClient(MineruConfig(token="t"), session=Session()).request_upload_urls("a.pdf")
    assert "page_ranges" not in captured["files"][0]


# --------------------------------------------------------------------------- #
# CLI 离线端到端
# --------------------------------------------------------------------------- #
def _make_pdf(page_texts: list[str]) -> bytes:
    n = len(page_texts)
    font_num = 3 + 2 * n
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(n))
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode(),
    ]
    for i, text in enumerate(page_texts):
        escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream = f"BT /F1 12 Tf 72 100 Td ({escaped}) Tj ET".encode()
        objects.append(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] "
            b"/Resources << /Font << /F1 " + str(font_num).encode() + b" 0 R >> >> "
            b"/Contents " + str(4 + 2 * i).encode() + b" 0 R >>"
        )
        objects.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    payload = b"%PDF-1.4\n"
    offsets = [0]
    for number, value in enumerate(objects, start=1):
        offsets.append(len(payload))
        payload += b"%d 0 obj\n" % number + value + b"\nendobj\n"
    xref = len(payload)
    payload += b"xref\n0 " + str(len(objects) + 1).encode() + b"\n0000000000 65535 f \n"
    payload += b"".join(b"%010d 00000 n \n" % off for off in offsets[1:])
    payload += b"trailer\n<< /Size " + str(len(objects) + 1).encode() + b" /Root 1 0 R >>\n"
    payload += b"startxref\n" + str(xref).encode() + b"\n%%EOF\n"
    return payload


def test_cli_offline_end_to_end(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    pdf = tmp_path / f"{DOC}.pdf"
    pdf.write_bytes(_make_pdf(["Page One Revenue", "Page Two Cost"]))

    exit_code = main(
        [
            "parse",
            str(pdf),
            "--offline",
            "--out",
            str(tmp_path / "out"),
            "--workdir",
            str(tmp_path / "work"),
            "--env-file",
            str(tmp_path / "none.env"),
        ]
    )

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "解析结果 : 2 页" in output

    doc_dir = tmp_path / "out" / DOC
    assert (doc_dir / "page_anchored.md").exists()
    report = json.loads((doc_dir / "report.json").read_text(encoding="utf-8"))
    assert report["pages"] == 2
    assert report["source"] == "pypdf-fallback"


def test_cli_fails_fast_without_token(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(_make_pdf(["only page"]))
    env = tmp_path / ".env"
    env.write_text("# 空配置\n", encoding="utf-8")

    for key in ("MINERU_API_TOKEN", "MINERU_TOKEN"):
        os.environ.pop(key, None)

    exit_code = main(
        [
            "parse",
            str(pdf),
            "--out",
            str(tmp_path / "out"),
            "--workdir",
            str(tmp_path / "work"),
            "--env-file",
            str(env),
        ]
    )

    assert exit_code == 3
    assert "MINERU_API_TOKEN" in capsys.readouterr().err


def test_cli_missing_pdf_returns_2(tmp_path: Path) -> None:
    exit_code = main(
        ["parse", str(tmp_path / "不存在.pdf"), "--offline", "--env-file", str(tmp_path / "e")]
    )
    assert exit_code == 2
