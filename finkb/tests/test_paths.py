"""知识库目录解析：FINKB_HOME > DSH 设置文件 > 包默认目录。"""

from __future__ import annotations

import json

from finkb.common import paths


def test_home_from_settings_file(tmp_path, monkeypatch):
    target = tmp_path / "workspace" / "finkb-data"
    target.mkdir(parents=True)
    settings = tmp_path / "finkb-settings.json"
    settings.write_text(json.dumps({"home": str(target)}), encoding="utf-8")
    monkeypatch.setenv("FINKB_SETTINGS_FILE", str(settings))
    monkeypatch.delenv("FINKB_HOME", raising=False)
    assert paths._home_from_settings_file() == target.resolve()


def test_bad_settings_file_is_ignored(tmp_path, monkeypatch):
    settings = tmp_path / "finkb-settings.json"
    settings.write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("FINKB_SETTINGS_FILE", str(settings))
    assert paths._home_from_settings_file() is None


def test_missing_settings_file_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setenv("FINKB_SETTINGS_FILE", str(tmp_path / "nope.json"))
    assert paths._home_from_settings_file() is None
