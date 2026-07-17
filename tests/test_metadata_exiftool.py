from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from faceit_ai.integration.metadata_port import MetadataWriteRequest
from faceit_ai.metadata.cleanup import (
    is_tool_owned_hierarchical,
    is_tool_owned_plain,
    tool_owned_hierarchical_from_list,
    tool_owned_plain_from_list,
)
from faceit_ai.metadata.exiftool_sync import (
    ExifToolMetadataSync,
    ExifToolWritePlan,
    _build_write_args,
    _exiftool_xmp_write_plan,
)
from faceit_ai.metadata.keyword_builder import (
    MetadataPayload,
    build_metadata_payload,
    normalize_gdpr_reason,
)
from faceit_ai.settings import (
    CollectSettings,
    DecisionSettings,
    ExportSettings,
    ImagePipelineSettings,
    IngestSettings,
    InsightFaceSettings,
    LightroomSettings,
    LoggingSettings,
    MatchingSettings,
    MetadataColorLabelSpec,
    MetadataIntegrationSettings,
    PipelineSettings,
    Settings,
)


def _minimal_settings(
    *,
    meta: MetadataIntegrationSettings,
    raw_ext: tuple[str, ...] = (".arw",),
) -> Settings:
    return Settings(
        database_url="sqlite:///:memory:",
        insightface_root=Path("/tmp"),
        logging=LoggingSettings(level="INFO", audit_log_path=Path("/tmp/a.log")),
        pipeline=PipelineSettings(
            insightface=InsightFaceSettings("m", (640, 640), ("CPU",)),
            image=ImagePipelineSettings(
                max_dimension=2000,
                supported_extensions=(".jpg",),
                raw_extensions=raw_ext,
                raw_decode_size="full",
                ignore_filename_substrings=(),
            ),
        ),
        matching=MatchingSettings(512.0, 300.0, 200.0),
        decision=DecisionSettings(0.6),
        export=ExportSettings("off", ("blocked", "review")),
        ingest=IngestSettings(enabled=False, destination_root=None, order="copy_then_analyze"),
        collect=CollectSettings(people_root=None),
        metadata=meta,
        lightroom=LightroomSettings(True, {"blocked": "red", "review": "purple", "ok": "none"}),
        usage_map={"social": "usage_social"},
    )


def test_normalize_gdpr_reason() -> None:
    assert normalize_gdpr_reason("unknown_face") == "unknown_person"


def test_tool_owned_prefixes() -> None:
    assert is_tool_owned_plain("sola/status/blocked")
    assert not is_tool_owned_plain("vacation")
    assert is_tool_owned_hierarchical("sola|status|blocked")
    assert not is_tool_owned_hierarchical("people|john")


def test_tool_owned_lists() -> None:
    assert tool_owned_plain_from_list(["sola/status/ok", "x"]) == ["sola/status/ok"]
    assert tool_owned_hierarchical_from_list(["sola|usage|social"]) == ["sola|usage|social"]


def test_exiftool_write_plan_uses_existing_canonical_sidecar(tmp_path: Path) -> None:
    raw = tmp_path / "2.ARW"
    raw.write_bytes(b"x")
    sc = tmp_path / "2.xmp"
    sc.write_text("<x:xmpmeta xmlns:x='adobe:ns:meta/'/>", encoding="utf-8")
    plan = _exiftool_xmp_write_plan(raw, "sidecar_raw")
    assert plan.read_target.resolve() == sc.resolve()
    assert plan.exiftool_source.resolve() == sc.resolve()
    assert plan.output_sidecar is None
    assert plan.tagging_kind == "sidecar_inplace"


def test_exiftool_write_plan_new_sidecar_via_raw_when_none(tmp_path: Path) -> None:
    raw = tmp_path / "2.ARW"
    raw.write_bytes(b"x")
    plan = _exiftool_xmp_write_plan(raw, "sidecar_raw")
    assert plan.read_target.resolve() == raw.resolve()
    assert plan.exiftool_source.resolve() == raw.resolve()
    assert plan.output_sidecar == tmp_path / "2.xmp"
    assert plan.tagging_kind == "sidecar_new_from_raw"


def test_exiftool_write_plan_strips_original_for_canonical_xmp(tmp_path: Path) -> None:
    raw = tmp_path / "seq_original.ARW"
    raw.write_bytes(b"x")
    plan = _exiftool_xmp_write_plan(raw, "sidecar_raw")
    assert plan.output_sidecar == tmp_path / "seq.xmp"
    assert plan.tagging_kind == "sidecar_new_from_raw"


def test_exiftool_write_plan_migrates_legacy_named_sidecar(tmp_path: Path) -> None:
    raw = tmp_path / "seq_original.ARW"
    raw.write_bytes(b"x")
    legacy = tmp_path / "seq_original.xmp"
    legacy.write_text("<x:xmpmeta xmlns:x='adobe:ns:meta/'/>", encoding="utf-8")
    plan = _exiftool_xmp_write_plan(raw, "sidecar_raw")
    assert plan.read_target.resolve() == legacy.resolve()
    assert plan.exiftool_source.resolve() == legacy.resolve()
    assert plan.output_sidecar == tmp_path / "seq.xmp"
    assert plan.tagging_kind == "sidecar_migrate_from_legacy"


def test_exiftool_write_plan_prefers_canonical_over_legacy(tmp_path: Path) -> None:
    raw = tmp_path / "seq_original.ARW"
    raw.write_bytes(b"x")
    canonical = tmp_path / "seq.xmp"
    canonical.write_text("<x:xmpmeta xmlns:x='adobe:ns:meta/'/>", encoding="utf-8")
    legacy = tmp_path / "seq_original.xmp"
    legacy.write_text("<x:xmpmeta xmlns:x='adobe:ns:meta/'/>", encoding="utf-8")
    plan = _exiftool_xmp_write_plan(raw, "sidecar_raw")
    assert plan.read_target.resolve() == canonical.resolve()
    assert plan.tagging_kind == "sidecar_inplace"


def test_build_metadata_payload_blocked() -> None:
    meta = MetadataIntegrationSettings(
        enabled=True,
        writer="exiftool",
        mode="sidecar_for_raw",
        exiftool_path="exiftool",
        exiftool_timeout_sec=30.0,
        dry_run=False,
        write_keywords=True,
        write_fields=True,
        write_photoshop_label_color=True,
        write_color_label=True,
        overwrite_color_labels=True,
        write_for_jpeg=False,
        write_for_tiff=False,
        preserve_unrelated_keywords=True,
        write_rating=False,
        overwrite_ratings=False,
        xmp_rating_by_status={},
        color_labels={
            "blocked": MetadataColorLabelSpec("Rot", "red"),
            "review": MetadataColorLabelSpec("Lila", "purple"),
            "ok": MetadataColorLabelSpec(None, None),
        },
        exiftool_config_path=None,
    )
    req = MetadataWriteRequest(
        file_path="/x/y.arw",
        status="blocked",
        reason="no_consent",
        usage="social",
        face_count=1,
        faces_identified=0,
        match_confidence_max=100.0,
    )
    p = build_metadata_payload(req, meta)
    assert p.xmp_label == "Rot"
    assert p.photoshop_label_color == "red"
    assert "sola/status/blocked" in p.plain_keywords
    assert "sola/reason/no_consent" in p.plain_keywords
    assert "sola/usage/social" in p.plain_keywords
    assert "sola|status|blocked" in p.hierarchical_keywords
    assert p.custom_fields.get("gdpr_status") == "blocked"


def test_build_metadata_payload_ok_clears_color() -> None:
    meta = MetadataIntegrationSettings(
        enabled=True,
        writer="exiftool",
        mode="sidecar_for_raw",
        exiftool_path="exiftool",
        exiftool_timeout_sec=30.0,
        dry_run=False,
        write_keywords=True,
        write_fields=False,
        write_photoshop_label_color=False,
        write_color_label=True,
        overwrite_color_labels=False,
        write_for_jpeg=False,
        write_for_tiff=False,
        preserve_unrelated_keywords=True,
        write_rating=False,
        overwrite_ratings=False,
        xmp_rating_by_status={},
        color_labels={
            "ok": MetadataColorLabelSpec(None, None),
        },
        exiftool_config_path=None,
    )
    req = MetadataWriteRequest(
        file_path="/a.arw", status="ok", reason="all_clear", usage="web"
    )
    p = build_metadata_payload(req, meta)
    assert p.clear_color_labels
    assert p.xmp_label is None


def test_build_write_args_contains_tags() -> None:
    meta = MetadataIntegrationSettings(
        enabled=True,
        writer="exiftool",
        mode="sidecar_for_raw",
        exiftool_path="exiftool",
        exiftool_timeout_sec=30.0,
        dry_run=False,
        write_keywords=True,
        write_fields=False,
        write_photoshop_label_color=True,
        write_color_label=True,
        overwrite_color_labels=True,
        write_for_jpeg=False,
        write_for_tiff=False,
        preserve_unrelated_keywords=True,
        write_rating=False,
        overwrite_ratings=False,
        xmp_rating_by_status={},
        color_labels={"blocked": MetadataColorLabelSpec("Red", "red")},
        exiftool_config_path=None,
    )
    payload = MetadataPayload(
        xmp_label="Red",
        photoshop_label_color="red",
        clear_color_labels=False,
        xmp_rating=None,
        plain_keywords=("sola/status/blocked",),
        hierarchical_keywords=("sola|status|blocked",),
        custom_fields={},
    )
    arw = Path("/tmp/x.arw")
    plan = ExifToolWritePlan(arw, arw, None, "embedded")
    cmd = _build_write_args(
        exiftool="/bin/exiftool",
        plan=plan,
        strategy="embedded",
        meta=meta,
        payload=payload,
        existing_label=None,
        existing_ps_color=None,
        existing_rating=None,
        subjects=["sola/status/review", "keepme"],
        hierarchical=["sola|status|review"],
    )
    assert cmd[0] == "/bin/exiftool"
    assert "-m" not in cmd
    assert any(a == "-XMP-dc:Subject-=sola/status/review" for a in cmd)
    assert any(a == "-XMP-dc:Subject+=sola/status/blocked" for a in cmd)
    assert any(a == "-XMP-lr:HierarchicalSubject-=sola|status|review" for a in cmd)
    assert any(a == "-XMP-lr:HierarchicalSubject+=sola|status|blocked" for a in cmd)
    assert "-XMP:Label=Red" in cmd
    assert "-Photoshop:LabelColor=red" in cmd
    assert cmd[-1] == str(arw)
    assert "-o" not in cmd
    assert "-overwrite_original_in_place" in cmd


def test_build_write_args_writes_label_when_overwrite_false_but_disk_differs() -> None:
    """LR often has xmp:Label without a matching Photoshop:LabelColor; old logic skipped both."""
    meta = MetadataIntegrationSettings(
        enabled=True,
        writer="exiftool",
        mode="sidecar_for_raw",
        exiftool_path="exiftool",
        exiftool_timeout_sec=30.0,
        dry_run=False,
        write_keywords=False,
        write_fields=False,
        write_photoshop_label_color=True,
        write_color_label=True,
        overwrite_color_labels=False,
        write_for_jpeg=False,
        write_for_tiff=False,
        preserve_unrelated_keywords=True,
        write_rating=False,
        overwrite_ratings=False,
        xmp_rating_by_status={},
        color_labels={"blocked": MetadataColorLabelSpec("Red", "red")},
        exiftool_config_path=None,
    )
    payload = MetadataPayload(
        xmp_label="Red",
        photoshop_label_color="red",
        clear_color_labels=False,
        xmp_rating=None,
        plain_keywords=(),
        hierarchical_keywords=(),
        custom_fields={},
    )
    arw = Path("/tmp/x.arw")
    plan = ExifToolWritePlan(arw, arw, None, "embedded")
    cmd = _build_write_args(
        exiftool="/bin/exiftool",
        plan=plan,
        strategy="embedded",
        meta=meta,
        payload=payload,
        existing_label="Rot",
        existing_ps_color=None,
        existing_rating=None,
        subjects=[],
        hierarchical=[],
    )
    assert "-XMP:Label=Red" in cmd
    assert "-Photoshop:LabelColor=red" in cmd


def test_build_write_args_skips_label_when_disk_matches_overwrite_false() -> None:
    meta = MetadataIntegrationSettings(
        enabled=True,
        writer="exiftool",
        mode="sidecar_for_raw",
        exiftool_path="exiftool",
        exiftool_timeout_sec=30.0,
        dry_run=False,
        write_keywords=False,
        write_fields=False,
        write_photoshop_label_color=True,
        write_color_label=True,
        overwrite_color_labels=False,
        write_for_jpeg=False,
        write_for_tiff=False,
        preserve_unrelated_keywords=True,
        write_rating=False,
        overwrite_ratings=False,
        xmp_rating_by_status={},
        color_labels={"blocked": MetadataColorLabelSpec("Rot", "red")},
        exiftool_config_path=None,
    )
    payload = MetadataPayload(
        xmp_label="Rot",
        photoshop_label_color="red",
        clear_color_labels=False,
        xmp_rating=None,
        plain_keywords=(),
        hierarchical_keywords=(),
        custom_fields={},
    )
    arw = Path("/tmp/x.arw")
    plan = ExifToolWritePlan(arw, arw, None, "embedded")
    cmd = _build_write_args(
        exiftool="/bin/exiftool",
        plan=plan,
        strategy="embedded",
        meta=meta,
        payload=payload,
        existing_label="Rot",
        existing_ps_color="red",
        existing_rating=None,
        subjects=[],
        hierarchical=[],
    )
    assert "-XMP:Label=Rot" not in cmd
    assert "-Photoshop:LabelColor=red" not in cmd


def test_build_write_args_reapplies_xmpmm_document_id_when_preserve_enabled() -> None:
    meta = MetadataIntegrationSettings(
        enabled=True,
        writer="exiftool",
        mode="sidecar_for_raw",
        exiftool_path="exiftool",
        exiftool_timeout_sec=30.0,
        dry_run=False,
        write_keywords=False,
        write_fields=False,
        write_photoshop_label_color=False,
        write_color_label=True,
        overwrite_color_labels=True,
        write_for_jpeg=False,
        write_for_tiff=False,
        preserve_unrelated_keywords=True,
        write_rating=False,
        overwrite_ratings=False,
        xmp_rating_by_status={},
        color_labels={"blocked": MetadataColorLabelSpec("Rot", "red")},
        exiftool_preserve_xmpmm_document_id=True,
        exiftool_preserve_xmpmm_instance_id=False,
        exiftool_config_path=None,
    )
    arw = Path("/tmp/x.arw")
    plan = ExifToolWritePlan(arw, arw, None, "embedded")
    payload = MetadataPayload(
        xmp_label="Rot",
        photoshop_label_color=None,
        clear_color_labels=False,
        xmp_rating=None,
        plain_keywords=(),
        hierarchical_keywords=(),
        custom_fields={},
    )
    uid = "urn:uuid:92d3c6ea-1e0b-4ae8-ac98-31ef9bbc9230"
    cmd = _build_write_args(
        exiftool="/bin/exiftool",
        plan=plan,
        strategy="embedded",
        meta=meta,
        payload=payload,
        existing_label=None,
        existing_ps_color=None,
        existing_rating=None,
        subjects=[],
        hierarchical=[],
        xmpmm_document_id=uid,
        xmpmm_original_document_id=None,
        xmpmm_instance_id=None,
    )
    assert f"-XMP-xmpMM:DocumentID={uid}" in cmd


def test_build_write_args_omits_xmpmm_override_when_preserve_disabled() -> None:
    meta = MetadataIntegrationSettings(
        enabled=True,
        writer="exiftool",
        mode="sidecar_for_raw",
        exiftool_path="exiftool",
        exiftool_timeout_sec=30.0,
        dry_run=False,
        write_keywords=False,
        write_fields=False,
        write_photoshop_label_color=False,
        write_color_label=True,
        overwrite_color_labels=True,
        write_for_jpeg=False,
        write_for_tiff=False,
        preserve_unrelated_keywords=True,
        write_rating=False,
        overwrite_ratings=False,
        xmp_rating_by_status={},
        color_labels={"blocked": MetadataColorLabelSpec("Rot", "red")},
        exiftool_preserve_xmpmm_document_id=False,
        exiftool_config_path=None,
    )
    arw = Path("/tmp/x.arw")
    plan = ExifToolWritePlan(arw, arw, None, "embedded")
    payload = MetadataPayload(
        xmp_label="Rot",
        photoshop_label_color=None,
        clear_color_labels=False,
        xmp_rating=None,
        plain_keywords=(),
        hierarchical_keywords=(),
        custom_fields={},
    )
    cmd = _build_write_args(
        exiftool="/bin/exiftool",
        plan=plan,
        strategy="embedded",
        meta=meta,
        payload=payload,
        existing_label=None,
        existing_ps_color=None,
        existing_rating=None,
        subjects=[],
        hierarchical=[],
        xmpmm_document_id="urn:uuid:92d3c6ea-1e0b-4ae8-ac98-31ef9bbc9230",
    )
    assert not any(a.startswith("-XMP-xmpMM:DocumentID") for a in cmd)


def test_build_write_args_sidecar_uses_dash_o_when_no_sidecar_yet() -> None:
    """Optional sidecar mode: ExifTool -o basename.xmp when creating from RAW (regression)."""
    meta = MetadataIntegrationSettings(
        enabled=True,
        writer="exiftool",
        mode="sidecar_for_raw",
        exiftool_path="exiftool",
        exiftool_timeout_sec=30.0,
        dry_run=False,
        write_keywords=True,
        write_fields=False,
        write_photoshop_label_color=True,
        write_color_label=True,
        overwrite_color_labels=True,
        write_for_jpeg=False,
        write_for_tiff=False,
        preserve_unrelated_keywords=True,
        write_rating=False,
        overwrite_ratings=False,
        xmp_rating_by_status={},
        color_labels={"blocked": MetadataColorLabelSpec("Red", "red")},
        exiftool_raw_target="sidecar",
        exiftool_config_path=None,
    )
    payload = MetadataPayload(
        xmp_label="Red",
        photoshop_label_color="red",
        clear_color_labels=False,
        xmp_rating=None,
        plain_keywords=("sola/status/blocked",),
        hierarchical_keywords=("sola|status|blocked",),
        custom_fields={},
    )
    arw = Path("/tmp/x.arw")
    plan = _exiftool_xmp_write_plan(arw, "sidecar_raw")
    cmd = _build_write_args(
        exiftool="/bin/exiftool",
        plan=plan,
        strategy="sidecar_raw",
        meta=meta,
        payload=payload,
        existing_label=None,
        existing_ps_color=None,
        existing_rating=None,
        subjects=["sola/status/review", "keepme"],
        hierarchical=["sola|status|review"],
    )
    assert "-o" in cmd
    assert cmd[-1] == str(arw)
    assert str(Path("/tmp/x.xmp")) == cmd[cmd.index("-o") + 1]


def test_build_write_args_inplace_when_sidecar_exists(tmp_path: Path) -> None:
    meta = MetadataIntegrationSettings(
        enabled=True,
        writer="exiftool",
        mode="sidecar_for_raw",
        exiftool_path="exiftool",
        exiftool_timeout_sec=30.0,
        dry_run=False,
        write_keywords=False,
        write_fields=False,
        write_photoshop_label_color=True,
        write_color_label=True,
        overwrite_color_labels=True,
        write_for_jpeg=False,
        write_for_tiff=False,
        preserve_unrelated_keywords=True,
        write_rating=False,
        overwrite_ratings=False,
        xmp_rating_by_status={},
        color_labels={"blocked": MetadataColorLabelSpec("Red", "red")},
        exiftool_raw_target="sidecar",
        exiftool_config_path=None,
    )
    raw = tmp_path / "p.ARW"
    raw.write_bytes(b"raw")
    sc = tmp_path / "p.xmp"
    sc.write_text("<x/>", encoding="utf-8")
    plan = _exiftool_xmp_write_plan(raw, "sidecar_raw")
    payload = MetadataPayload(
        xmp_label="Red",
        photoshop_label_color="red",
        clear_color_labels=False,
        xmp_rating=None,
        plain_keywords=(),
        hierarchical_keywords=(),
        custom_fields={},
    )
    cmd = _build_write_args(
        exiftool="/bin/exiftool",
        plan=plan,
        strategy="sidecar_raw",
        meta=meta,
        payload=payload,
        existing_label=None,
        existing_ps_color=None,
        existing_rating=None,
        subjects=[],
        hierarchical=[],
    )
    assert "-overwrite_original_in_place" in cmd
    assert "-o" not in cmd
    assert cmd[-1] == str(sc)


@pytest.mark.parametrize("which", ["missing", "timeout", "error"])
def test_exiftool_apply_resilient(which: str) -> None:
    meta = MetadataIntegrationSettings(
        enabled=True,
        writer="exiftool",
        mode="sidecar_for_raw",
        exiftool_path="exiftool",
        exiftool_timeout_sec=1.0,
        dry_run=False,
        write_keywords=True,
        write_fields=False,
        write_photoshop_label_color=False,
        write_color_label=False,
        overwrite_color_labels=True,
        write_for_jpeg=False,
        write_for_tiff=False,
        preserve_unrelated_keywords=True,
        write_rating=False,
        overwrite_ratings=False,
        xmp_rating_by_status={},
        color_labels={},
        exiftool_config_path=None,
    )
    settings = _minimal_settings(meta=meta)
    sync = ExifToolMetadataSync(settings, log=MagicMock(), audit=None)
    fake_path = Path(__file__).resolve()

    if which == "missing":
        with patch(
            "faceit_ai.metadata.exiftool_sync._resolve_exiftool_bin", return_value=None
        ):
            sync.apply(
                MetadataWriteRequest(
                    file_path=str(fake_path.with_suffix(".arw")),
                    status="ok",
                    reason="all_clear",
                    usage="social",
                )
            )
    elif which == "timeout":
        with patch(
            "faceit_ai.metadata.exiftool_sync._resolve_exiftool_bin", return_value="/x/exiftool"
        ), patch(
            "faceit_ai.metadata.exiftool_sync._read_xmp_tags",
            side_effect=subprocess.TimeoutExpired("x", 1),
        ):
            sync.apply(
                MetadataWriteRequest(
                    file_path=str(fake_path.with_suffix(".arw")),
                    status="ok",
                    reason="all_clear",
                    usage="social",
                )
            )
    else:
        with patch(
            "faceit_ai.metadata.exiftool_sync._resolve_exiftool_bin", return_value="/x/exiftool"
        ), patch(
            "faceit_ai.metadata.exiftool_sync.subprocess.run",
            side_effect=OSError("boom"),
        ):
            sync.apply(
                MetadataWriteRequest(
                    file_path=str(fake_path.with_suffix(".arw")),
                    status="ok",
                    reason="all_clear",
                    usage="social",
                )
            )


def test_build_metadata_payload_xmp_label_values_override() -> None:
    meta = MetadataIntegrationSettings(
        enabled=True,
        writer="exiftool",
        mode="sidecar_for_raw",
        exiftool_path="exiftool",
        exiftool_timeout_sec=30.0,
        dry_run=False,
        write_keywords=True,
        write_fields=False,
        write_photoshop_label_color=False,
        write_color_label=True,
        overwrite_color_labels=True,
        write_for_jpeg=False,
        write_for_tiff=False,
        preserve_unrelated_keywords=True,
        write_rating=False,
        overwrite_ratings=False,
        xmp_rating_by_status={},
        color_labels={
            "blocked": MetadataColorLabelSpec("Wrong", "red"),
        },
        exiftool_config_path=None,
    )
    lr = LightroomSettings(
        True,
        {"blocked": "red", "review": "purple", "ok": "none"},
        {"blocked": "ExactCatalogString"},
    )
    req = MetadataWriteRequest(
        file_path="/x/y.arw",
        status="blocked",
        reason="no_consent",
        usage="social",
    )
    p = build_metadata_payload(req, meta, lr)
    assert p.xmp_label == "ExactCatalogString"
    assert p.photoshop_label_color is None


def test_build_write_args_writes_xmp_rating() -> None:
    meta = MetadataIntegrationSettings(
        enabled=True,
        writer="exiftool",
        mode="sidecar_for_raw",
        exiftool_path="exiftool",
        exiftool_timeout_sec=30.0,
        dry_run=False,
        write_keywords=False,
        write_fields=False,
        write_photoshop_label_color=False,
        write_color_label=False,
        overwrite_color_labels=True,
        write_for_jpeg=False,
        write_for_tiff=False,
        preserve_unrelated_keywords=True,
        write_rating=True,
        overwrite_ratings=True,
        xmp_rating_by_status={},
        color_labels={},
        exiftool_config_path=None,
    )
    payload = MetadataPayload(
        xmp_label=None,
        photoshop_label_color=None,
        clear_color_labels=False,
        xmp_rating=3,
        plain_keywords=(),
        hierarchical_keywords=(),
        custom_fields={},
    )
    arw = Path("/tmp/x.arw")
    plan = ExifToolWritePlan(arw, arw, None, "embedded")
    cmd = _build_write_args(
        exiftool="/bin/exiftool",
        plan=plan,
        strategy="embedded",
        meta=meta,
        payload=payload,
        existing_label=None,
        existing_ps_color=None,
        existing_rating=1,
        subjects=[],
        hierarchical=[],
    )
    assert "-XMP:Rating=3" in cmd


def test_build_write_args_omits_photoshop_when_disabled() -> None:
    meta = MetadataIntegrationSettings(
        enabled=True,
        writer="exiftool",
        mode="sidecar_for_raw",
        exiftool_path="exiftool",
        exiftool_timeout_sec=30.0,
        dry_run=False,
        write_keywords=False,
        write_fields=False,
        write_photoshop_label_color=False,
        write_color_label=True,
        overwrite_color_labels=True,
        write_for_jpeg=False,
        write_for_tiff=False,
        preserve_unrelated_keywords=True,
        write_rating=False,
        overwrite_ratings=False,
        xmp_rating_by_status={},
        color_labels={"blocked": MetadataColorLabelSpec("Red", "red")},
        exiftool_config_path=None,
    )
    payload = MetadataPayload(
        xmp_label="Red",
        photoshop_label_color=None,
        clear_color_labels=False,
        xmp_rating=None,
        plain_keywords=(),
        hierarchical_keywords=(),
        custom_fields={},
    )
    arw = Path("/tmp/x.arw")
    plan = ExifToolWritePlan(arw, arw, None, "embedded")
    cmd = _build_write_args(
        exiftool="/bin/exiftool",
        plan=plan,
        strategy="embedded",
        meta=meta,
        payload=payload,
        existing_label=None,
        existing_ps_color=None,
        existing_rating=None,
        subjects=[],
        hierarchical=[],
    )
    assert "-XMP:Label=Red" in cmd
    assert not any(a.startswith("-Photoshop:LabelColor") for a in cmd)


def test_exiftool_skip_non_raw_when_not_configured() -> None:
    meta = MetadataIntegrationSettings(
        enabled=True,
        writer="exiftool",
        mode="sidecar_for_raw",
        exiftool_path="exiftool",
        exiftool_timeout_sec=30.0,
        dry_run=False,
        write_keywords=True,
        write_fields=False,
        write_photoshop_label_color=False,
        write_color_label=False,
        overwrite_color_labels=True,
        write_for_jpeg=False,
        write_for_tiff=False,
        preserve_unrelated_keywords=True,
        write_rating=False,
        overwrite_ratings=False,
        xmp_rating_by_status={},
        color_labels={},
        exiftool_config_path=None,
    )
    settings = _minimal_settings(meta=meta)
    log = MagicMock()
    sync = ExifToolMetadataSync(settings, log=log, audit=None)
    jpg = Path(__file__).with_suffix(".jpg")
    sync.apply(MetadataWriteRequest(file_path=str(jpg), status="ok", reason="all_clear", usage="social"))
    log.debug.assert_called()
