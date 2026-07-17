from __future__ import annotations

from pathlib import Path

import pytest

from faceit_ai.integration.metadata_port import MetadataWriteRequest, XmpSidecarMetadataSync
from faceit_ai.integration.xmp_sidecar import build_xmp_packet, sidecar_path_for_image
from faceit_ai.metadata.keyword_builder import normalize_gdpr_reason
from faceit_ai.settings import (
    LightroomSettings,
    MetadataColorLabelSpec,
    MetadataIntegrationSettings,
)


def test_normalize_gdpr_reason_maps_unknown_face() -> None:
    assert normalize_gdpr_reason("unknown_face") == "unknown_person"
    assert normalize_gdpr_reason("uncertain_match") == "low_confidence"
    assert normalize_gdpr_reason("no_consent") == "no_consent"


def test_build_xmp_contains_label_status_keywords() -> None:
    req = MetadataWriteRequest(
        file_path="/tmp/x.arw",
        status="blocked",
        reason="no_consent",
        usage="social",
        face_count=2,
        faces_identified=1,
        match_confidence_max=300.5,
    )
    xml = build_xmp_packet(
        req=req,
        color_label_lightroom="Red",
        write_label=True,
        overwrite_label=True,
        write_keywords=True,
        write_fields=True,
        existing_xml=None,
    )
    assert "Red" in xml
    assert "sola/status/blocked" in xml
    assert "sola/reason/no_consent" in xml
    assert "sola/usage/social" in xml
    assert "sola|status|blocked" in xml
    assert "sola|usage|social" in xml
    assert "gdpr_status" in xml or "gdpr_status" in xml.replace(":", "")
    assert "social" in xml


def test_build_xmp_merge_replaces_sola_keywords_keeps_others() -> None:
    existing = """<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/">
  <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
    <rdf:Description rdf:about=""
        xmlns:dc="http://purl.org/dc/elements/1.1/">
      <dc:subject>
        <rdf:Bag>
          <rdf:li>vacation</rdf:li>
          <rdf:li>sola/status/review</rdf:li>
        </rdf:Bag>
      </dc:subject>
    </rdf:Description>
  </rdf:RDF>
</x:xmpmeta>
<?xpacket end="w"?>"""
    second = build_xmp_packet(
        req=MetadataWriteRequest(
            file_path="/a/test.arw",
            status="blocked",
            reason="no_consent",
            usage="social",
        ),
        color_label_lightroom="Red",
        write_label=True,
        overwrite_label=True,
        write_keywords=True,
        write_fields=True,
        existing_xml=existing,
    )
    assert "vacation" in second
    assert "sola/status/blocked" in second
    assert "sola/status/review" not in second


def test_sidecar_path() -> None:
    assert sidecar_path_for_image(Path("/d/f.ARW")) == Path("/d/f.xmp")


def test_xmp_sync_writes_file(tmp_path: Path) -> None:
    img = tmp_path / "shot.jpg"
    img.write_bytes(b"fake")
    meta = MetadataIntegrationSettings(
        enabled=True,
        writer="xmp_manual",
        mode="xmp_sidecar",
        exiftool_path="exiftool",
        exiftool_timeout_sec=120.0,
        dry_run=False,
        write_keywords=True,
        write_fields=True,
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
            "blocked": MetadataColorLabelSpec("Red", "red"),
            "review": MetadataColorLabelSpec("Purple", "purple"),
            "ok": MetadataColorLabelSpec(None, None),
        },
        exiftool_config_path=None,
    )
    lr = LightroomSettings(
        enable=True,
        color_labels={"blocked": "red", "review": "purple", "ok": "none"},
    )
    sync = XmpSidecarMetadataSync(meta, lr)
    sync.apply(
        MetadataWriteRequest(
            file_path=str(img),
            status="ok",
            reason="all_clear",
            usage="social",
            face_count=0,
            faces_identified=0,
            match_confidence_max=None,
        )
    )
    xmp = img.with_suffix(".xmp")
    assert xmp.is_file()
    body = xmp.read_text(encoding="utf-8")
    assert "sola/status/ok" in body


@pytest.mark.parametrize(
    "internal,public",
    [
        ("unknown_face", "unknown_person"),
        ("no_consent", "no_consent"),
    ],
)
def test_reason_roundtrip_in_packet(internal: str, public: str) -> None:
    req = MetadataWriteRequest(
        file_path="/x",
        status="review",
        reason=internal,
        usage="web",
    )
    xml = build_xmp_packet(
        req=req,
        color_label_lightroom=None,
        write_label=False,
        overwrite_label=False,
        write_keywords=True,
        write_fields=True,
        existing_xml=None,
    )
    assert f"sola/reason/{public}" in xml
