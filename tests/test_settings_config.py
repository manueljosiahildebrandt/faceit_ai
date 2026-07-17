"""Config path resolution (ensures repo ``config/default.yaml`` is used when appropriate)."""

from __future__ import annotations

from pathlib import Path

import pytest

from faceit_ai.settings import load_raw_config, resolve_config_path


def test_resolve_config_prefers_data_dir_over_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    (repo / "config").mkdir(parents=True)
    (repo / "config" / "default.yaml").write_text("lightroom:\n  xmp_label_values:\n    blocked: FromRepo\n", encoding="utf-8")
    data = tmp_path / "data"
    (data / "config").mkdir(parents=True)
    (data / "config" / "default.yaml").write_text(
        "lightroom:\n  xmp_label_values:\n    blocked: FromDataDir\n", encoding="utf-8"
    )
    monkeypatch.chdir(repo)
    monkeypatch.setenv("FACEIT_AI_DATA_DIR", str(data))
    assert resolve_config_path() == data / "config" / "default.yaml"
    raw = load_raw_config()
    assert raw["lightroom"]["xmp_label_values"]["blocked"] == "FromDataDir"


def test_resolve_config_walks_up_from_subdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "myproject"
    sub = repo / "deep" / "nested"
    sub.mkdir(parents=True)
    (repo / "config").mkdir()
    (repo / "config" / "default.yaml").write_text("pipeline:\n  image:\n    max_dimension: 9999\n", encoding="utf-8")
    monkeypatch.chdir(sub)
    monkeypatch.delenv("FACEIT_AI_DATA_DIR", raising=False)
    monkeypatch.delenv("FACEIT_AI_CONFIG", raising=False)
    path = resolve_config_path()
    assert path == repo / "config" / "default.yaml"
    raw = load_raw_config()
    assert raw["pipeline"]["image"]["max_dimension"] == 9999
