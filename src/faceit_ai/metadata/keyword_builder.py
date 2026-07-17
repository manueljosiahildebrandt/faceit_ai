"""Plain and hierarchical keywords from ``MetadataWriteRequest`` (``sola/…`` and ``sola|…``)."""

from __future__ import annotations

from dataclasses import dataclass

from faceit_ai.integration.metadata_port import MetadataWriteRequest
from faceit_ai.settings import LightroomSettings, MetadataColorLabelSpec, MetadataIntegrationSettings


def normalize_gdpr_reason(internal_reason: str) -> str:
    m = {
        "unknown_face": "unknown_person",
        "uncertain_match": "low_confidence",
        "low_confidence": "low_confidence",
        "no_consent": "no_consent",
        "usage_not_allowed": "no_consent",
        "all_clear": "all_clear",
        "no_faces": "unknown_person",
        "mixed_group": "mixed_group",
    }
    return m.get(internal_reason, internal_reason)


def usage_keyword_token(usage: str) -> str:
    u = usage.strip().lower().replace(" ", "_")
    return u or "unknown"


@dataclass(frozen=True)
class MetadataPayload:
    xmp_label: str | None
    photoshop_label_color: str | None
    clear_color_labels: bool
    # 0–5 for XMP:Rating; None = do not write (preserve existing stars).
    xmp_rating: int | None
    plain_keywords: tuple[str, ...]
    hierarchical_keywords: tuple[str, ...]
    custom_fields: dict[str, str | int | float | None]


def _label_spec_for_status(
    meta: MetadataIntegrationSettings, status: str
) -> MetadataColorLabelSpec | None:
    return meta.color_labels.get(status.lower())


def build_metadata_payload(
    req: MetadataWriteRequest,
    meta: MetadataIntegrationSettings,
    lightroom: LightroomSettings | None = None,
) -> MetadataPayload:
    spec = _label_spec_for_status(meta, req.status)
    st = req.status.lower()
    if lightroom is not None and st in lightroom.xmp_label_values:
        raw = lightroom.xmp_label_values[st]
        xs = str(raw).strip()
        if xs == "" or xs.lower() in ("none", "null", "~"):
            xmp_label: str | None = None
        else:
            xmp_label = xs
    elif spec is None:
        xmp_label = None
    else:
        xmp_label = spec.xmp_label

    if spec is not None and meta.write_photoshop_label_color:
        ps_color = spec.photoshop_label_color
    else:
        ps_color = None

    clear_color = bool(meta.write_color_label) and xmp_label is None and ps_color is None

    xmp_rating: int | None = None
    if meta.write_rating and st in meta.xmp_rating_by_status:
        xmp_rating = max(0, min(5, int(meta.xmp_rating_by_status[st])))

    pub = normalize_gdpr_reason(req.reason)
    usage_s = usage_keyword_token(req.usage)
    plain = (
        f"sola/status/{req.status}",
        f"sola/reason/{pub}",
        f"sola/usage/{usage_s}",
    )
    hier = (
        f"sola|status|{req.status}",
        f"sola|reason|{pub}",
        f"sola|usage|{usage_s}",
    )

    custom: dict[str, str | int | float | None] = {}
    if meta.write_fields:
        custom["gdpr_status"] = req.status
        custom["gdpr_reason"] = pub
        custom["gdpr_usage"] = req.usage
        if req.face_count is not None:
            custom["faces_detected"] = int(req.face_count)
        if req.faces_identified is not None:
            custom["faces_identified"] = int(req.faces_identified)
        if req.match_confidence_max is not None:
            custom["match_confidence_max"] = float(req.match_confidence_max)

    return MetadataPayload(
        xmp_label=xmp_label,
        photoshop_label_color=ps_color,
        clear_color_labels=clear_color,
        xmp_rating=xmp_rating,
        plain_keywords=plain,
        hierarchical_keywords=hier,
        custom_fields=custom,
    )
