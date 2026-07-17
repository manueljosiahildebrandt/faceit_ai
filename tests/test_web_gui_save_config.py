"""Settings save from web UI must persist database URL together with pipeline fields."""

from __future__ import annotations

from pathlib import Path

import yaml

from faceit_ai import web_gui


def _minimal_yaml() -> dict:
    return {
        "database": {"url": "", "sqlite_relative_path": "data/consent.db"},
        "paths": {"data_dir": ""},
        "pipeline": {
            "insightface": {"det_size": [512, 512]},
            "image": {"max_dimension": 1800, "raw_decode_size": "half"},
        },
        "metadata": {"enabled": True, "exiftool_verify_after_write": False, "exiftool_path": "exiftool"},
        "logging": {"level": "INFO"},
        "lightroom": {"xmp_label_values": {"blocked": "Rot", "review": "Lila", "ok": ""}},
    }


def _full_settings_form(*, database_url: str = "") -> dict[str, str]:
    return {
        "det_size": "512,512",
        "max_dimension": "1800",
        "raw_decode_size": "half",
        "sync_metadata_default": "on",
        "verify_after_write": "",
        "exiftool_path": "exiftool",
        "debug_logging": "",
        "data_dir": "",
        "database_url": database_url,
        "label_blocked": "Rot",
        "label_review": "Lila",
        "label_ok": "None",
        "ingest_enabled": "",
        "ingest_order": "copy_then_analyze",
        "force_default": "",
        "export_flagged": "off",
        "export_status_blocked": "",
        "export_status_review": "",
        "collect_crop_portrait": "",
        "inference_providers": "auto",
    }


def test_save_config_persists_database_url(tmp_path: Path) -> None:
    cfg_path = tmp_path / "default.yaml"
    cfg_path.write_text(yaml.safe_dump(_minimal_yaml(), sort_keys=False), encoding="utf-8")
    web_gui.STATE.config_path = cfg_path

    pg = "postgresql+psycopg://facit:example@db.example.com:5432/faceit_ai"
    msg = web_gui._save_config(_full_settings_form(database_url=pg))

    assert "Saved settings" in msg
    saved = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    assert saved["database"]["url"] == pg
    assert saved["pipeline"]["insightface"]["det_size"] == [512, 512]
    assert saved["metadata"]["enabled"] is True
    assert saved["analyze"]["sync_metadata_default"] is True


def test_save_config_does_not_clear_database_url_when_field_present(tmp_path: Path) -> None:
    raw = _minimal_yaml()
    raw["database"]["url"] = "postgresql+psycopg://existing@host/db"
    cfg_path = tmp_path / "default.yaml"
    cfg_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    web_gui.STATE.config_path = cfg_path

    web_gui._save_config(_full_settings_form(database_url=raw["database"]["url"]))

    saved = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    assert saved["database"]["url"] == raw["database"]["url"]


def test_save_config_persists_ingest_enabled(tmp_path: Path) -> None:
    cfg_path = tmp_path / "default.yaml"
    cfg_path.write_text(yaml.safe_dump(_minimal_yaml(), sort_keys=False), encoding="utf-8")
    web_gui.STATE.config_path = cfg_path

    form = _full_settings_form()
    form["ingest_enabled"] = "on"

    msg = web_gui._save_config(form)

    assert "Saved settings" in msg
    saved = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    assert saved["ingest"]["enabled"] is True


def test_save_config_persists_raw_decode_size(tmp_path: Path) -> None:
    cfg_path = tmp_path / "default.yaml"
    cfg_path.write_text(yaml.safe_dump(_minimal_yaml(), sort_keys=False), encoding="utf-8")
    web_gui.STATE.config_path = cfg_path

    form = _full_settings_form()
    form["raw_decode_size"] = "quarter"

    web_gui._save_config(form)

    saved = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    assert saved["pipeline"]["image"]["raw_decode_size"] == "quarter"
    assert "raw_half_size" not in saved["pipeline"]["image"]


def test_save_config_persists_ingest_order(tmp_path: Path) -> None:
    cfg_path = tmp_path / "default.yaml"
    cfg_path.write_text(yaml.safe_dump(_minimal_yaml(), sort_keys=False), encoding="utf-8")
    web_gui.STATE.config_path = cfg_path

    form = _full_settings_form()
    form["ingest_enabled"] = "on"
    form["ingest_order"] = "analyze_then_copy"

    web_gui._save_config(form)

    saved = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    assert saved["ingest"]["order"] == "analyze_then_copy"
