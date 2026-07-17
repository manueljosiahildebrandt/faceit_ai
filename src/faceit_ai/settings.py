"""Load and validate YAML configuration with deterministic path resolution."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

IngestOrder = Literal["copy_then_analyze", "analyze_then_copy"]
RawDecodeSize = Literal["full", "half", "quarter"]

import yaml

from faceit_ai.inference.providers import resolve_onnx_providers


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in override.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _data_root() -> Path:
    return Path(os.environ.get("FACEIT_AI_DATA_DIR", ".")).resolve()


def _resolve_data_root(raw: dict[str, Any]) -> Path:
    """Data root precedence: FACEIT_AI_DATA_DIR env > paths.data_dir in config > cwd.

    Config discovery in ``resolve_config_path`` still uses env/cwd only (chicken-and-egg),
    but once the config is loaded the DB/log roots may honor ``paths.data_dir``.
    """
    env = os.environ.get("FACEIT_AI_DATA_DIR")
    if env:
        return Path(env).expanduser().resolve()
    cfg_dir = str((raw.get("paths") or {}).get("data_dir") or "").strip()
    if cfg_dir:
        return Path(os.path.expandvars(cfg_dir)).expanduser().resolve()
    return Path(".").resolve()


def resolve_config_path(explicit: Path | None = None) -> Path:
    if explicit is not None:
        return explicit
    env = os.environ.get("FACEIT_AI_CONFIG")
    if env:
        return Path(env).expanduser().resolve()

    def _pick(p: Path) -> Path | None:
        return p if p.is_file() else None

    # Data dir (e.g. FACEIT_AI_DATA_DIR) often holds a deployable copy of config.
    hit = _pick(_data_root() / "config" / "default.yaml")
    if hit is not None:
        return hit

    # Walk upward from cwd so `pip install` (non-editable) still finds the repo’s
    # config/default.yaml when you run the CLI from the project tree.
    cwd = Path.cwd().resolve()
    for d in (cwd, *cwd.parents):
        hit = _pick(d / "config" / "default.yaml")
        if hit is not None:
            return hit

    # Editable install: faceit_ai lives under src/, repo root is parents[2].
    legacy = Path(__file__).resolve().parents[2] / "config" / "default.yaml"
    if legacy.is_file():
        return legacy

    return Path(__file__).resolve().parent / "default_config.yaml"


def load_raw_config(path: Path | None = None) -> dict[str, Any]:
    cfg_path = resolve_config_path(path)
    with open(cfg_path, encoding="utf-8") as f:
        data: dict[str, Any] = yaml.safe_load(f) or {}
    user = os.environ.get("FACEIT_AI_CONFIG_EXTRA")
    if user:
        with open(user, encoding="utf-8") as f:
            extra = yaml.safe_load(f) or {}
        data = _deep_merge(data, extra)
    return data


def parse_raw_decode_size(img: dict[str, Any]) -> RawDecodeSize:
    """Resolve RAW decode size from config (supports legacy raw_half_size bool)."""
    raw = str(img.get("raw_decode_size", "")).strip().lower()
    if raw in ("full", "half", "quarter"):
        return raw  # type: ignore[return-value]
    if bool(img.get("raw_half_size", False)):
        return "half"
    return "full"


@dataclass(frozen=True)
class InsightFaceSettings:
    model_name: str
    det_size: tuple[int, int]
    providers: tuple[str, ...]


@dataclass(frozen=True)
class ImagePipelineSettings:
    max_dimension: int
    supported_extensions: tuple[str, ...]
    raw_extensions: tuple[str, ...]
    raw_decode_size: RawDecodeSize
    # If basename contains any of these substrings (case-insensitive), file is skipped entirely.
    ignore_filename_substrings: tuple[str, ...]

    def scan_extensions(self) -> tuple[str, ...]:
        return tuple(sorted(set(self.supported_extensions) | set(self.raw_extensions)))


@dataclass(frozen=True)
class PipelineSettings:
    insightface: InsightFaceSettings
    image: ImagePipelineSettings


@dataclass(frozen=True)
class MatchingSettings:
    """
    Gallery match score = cosine_similarity(query, gallery_vec) * match_score_scale.

    Thresholds are compared to that scaled score (defaults align ~InsightFace cosine * 512
    so values like 200–400 are usable in configs and logs).
    """

    match_score_scale: float
    match_threshold_strong: float
    match_threshold_review: float


@dataclass(frozen=True)
class DecisionSettings:
    """Image-level decision policy after matching.

    ``unknown_face_status``: what to do with faces that match nobody above the
    review threshold. ``ok`` = only block known non-consented people (typical
    publishing workflow). ``review`` = send every stranger face to review.
    """

    min_confident_match: float
    unknown_face_status: Literal["ok", "review"] = "ok"


@dataclass(frozen=True)
class ExportSettings:
    flagged: Literal["off", "copy", "move"]
    flagged_status: tuple[str, ...]


@dataclass(frozen=True)
class IngestSettings:
    """Optional archive copy of the entire source folder (e.g. SD card → NAS)."""

    enabled: bool
    destination_root: Path | None
    order: IngestOrder = "copy_then_analyze"


@dataclass(frozen=True)
class CollectSettings:
    """Optional destination for copying matched photos into people folders.

    When set, ``analyze_photos`` copies each photo with a face match score
    >= ``match_threshold_collect`` into ``<people_root>/<person_name>/``
    (unless ``--collect-to`` overrides the root). ``None`` disables collection.

    ``match_threshold_collect`` is intentionally below ``match_threshold_strong``
    (and a bit above ``match_threshold_review``) so collect catches more of the
    faces that flagged/review already finds, without changing GDPR decisions.
    """

    people_root: Path | None
    crop_portrait: bool = False
    crop_aspect_w: float = 3.0
    crop_aspect_h: float = 4.0
    crop_padding: float = 1.5
    output_format: Literal["jpg"] = "jpg"
    # Scaled score (same units as matching thresholds). Default ~just above review.
    match_threshold_collect: float = 240.0


@dataclass(frozen=True)
class MetadataColorLabelSpec:
    """``XMP:Label`` text plus optional ``Photoshop:LabelColor`` (secondary; LR reads label text)."""

    xmp_label: str | None
    photoshop_label_color: str | None


@dataclass(frozen=True)
class MetadataIntegrationSettings:
    enabled: bool
    # exiftool (default) | xmp_manual legacy XML sidecar writer
    writer: Literal["exiftool", "xmp_manual"]
    mode: Literal["sidecar_for_raw", "xmp_sidecar", "jpeg_embed"]
    exiftool_path: str
    exiftool_timeout_sec: float
    dry_run: bool
    write_keywords: bool
    write_fields: bool
    # Pair with XMP:Label like Lightroom (photoshop:LabelColor token, e.g. red).
    write_photoshop_label_color: bool
    write_color_label: bool
    overwrite_color_labels: bool
    write_for_jpeg: bool
    write_for_tiff: bool
    preserve_unrelated_keywords: bool
    color_labels: dict[str, MetadataColorLabelSpec]
    # XMP:Rating 0–5; omit status keys in xmp_rating_by_status to leave stars unchanged.
    write_rating: bool
    overwrite_ratings: bool
    xmp_rating_by_status: dict[str, int]
    # RAW: ``embedded`` = XMP in the camera file (ARW/DNG/…); ``sidecar`` = adjacent ``.xmp`` only.
    exiftool_raw_target: Literal["embedded", "sidecar"] = "embedded"
    # Re-apply existing Adobe XMP-MM IDs so ExifTool does not mint new UUIDs (Lightroom document identity).
    exiftool_preserve_xmpmm_document_id: bool = True
    exiftool_preserve_xmpmm_instance_id: bool = False
    # If set, passed as ``exiftool -config`` so custom ``sola:*`` XMP tags can be written.
    exiftool_config_path: Path | None = None
    # Second ExifTool read after each write; slow and often false-alarms (e.g. Photoshop:LabelColor on RAW).
    exiftool_verify_after_write: bool = False


@dataclass(frozen=True)
class LightroomSettings:
    enable: bool
    color_labels: dict[str, str]
    """Exact strings written to ``XMP:Label`` per status (catalog-dependent). Overrides derived labels."""
    xmp_label_values: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class LoggingSettings:
    level: str
    audit_log_path: Path


@dataclass(frozen=True)
class Settings:
    database_url: str
    insightface_root: Path
    logging: LoggingSettings
    pipeline: PipelineSettings
    matching: MatchingSettings
    decision: DecisionSettings
    export: ExportSettings
    ingest: IngestSettings
    collect: CollectSettings
    metadata: MetadataIntegrationSettings
    lightroom: LightroomSettings
    usage_map: dict[str, str] = field(default_factory=dict)

    @staticmethod
    def from_dict(raw: dict[str, Any]) -> Settings:
        data_root = _resolve_data_root(raw)
        db_cfg = raw.get("database") or {}
        # Explicit SQLAlchemy URL wins (e.g. Postgres for shared multi-PC use).
        # ${ENV} expansion lets the DB password live in an env var, not the YAML.
        url_cfg = os.path.expandvars(str(db_cfg.get("url") or "")).strip()
        if url_cfg:
            database_url = url_cfg
        else:
            db_rel = db_cfg["sqlite_relative_path"]
            db_file = (data_root / db_rel).resolve()
            db_file.parent.mkdir(parents=True, exist_ok=True)
            database_url = f"sqlite:///{db_file}"

        paths = raw["paths"]
        ifr = (paths.get("insightface_root") or "").strip()
        if ifr:
            insightface_root = Path(ifr).expanduser().resolve()
        else:
            insightface_root = Path(
                os.environ.get("FACEIT_AI_MODEL_ROOT", Path.home() / ".insightface")
            ).expanduser().resolve()

        log_dir = (data_root / raw["paths"]["log_relative_dir"]).resolve()
        log_dir.mkdir(parents=True, exist_ok=True)
        audit_path = log_dir / raw["logging"]["audit_filename"]

        pi = raw["pipeline"]["insightface"]
        det = pi["det_size"]
        insightface = InsightFaceSettings(
            model_name=pi["model_name"],
            det_size=(int(det[0]), int(det[1])),
            providers=resolve_onnx_providers(tuple(pi.get("providers") or ("auto",))),
        )
        img = raw["pipeline"]["image"]
        raw_ext = tuple(str(x).lower() for x in img.get("raw_extensions", []))
        ign = img.get("ignore_filename_substrings")
        if ign is None:
            ign = ["-pano"]  # Lightroom merge/panorama DNGs; LibRaw cannot decode them
        pipeline = PipelineSettings(
            insightface=insightface,
            image=ImagePipelineSettings(
                max_dimension=int(img["max_dimension"]),
                supported_extensions=tuple(str(x).lower() for x in img["supported_extensions"]),
                raw_extensions=raw_ext,
                raw_decode_size=parse_raw_decode_size(img),
                ignore_filename_substrings=tuple(str(x) for x in ign),
            ),
        )

        m = raw["matching"]
        if "match_threshold_strong" in m or "match_score_scale" in m:
            matching = MatchingSettings(
                match_score_scale=float(m.get("match_score_scale", 512.0)),
                match_threshold_strong=float(m["match_threshold_strong"]),
                match_threshold_review=float(m["match_threshold_review"]),
            )
        else:
            matching = MatchingSettings(
                match_score_scale=1.0,
                match_threshold_strong=float(m["strong_match_min"]),
                match_threshold_review=float(m["uncertain_min"]),
            )

        d = raw.get("decision") or {}
        unk_raw = str(d.get("unknown_face_status", "ok")).strip().lower()
        if unk_raw not in ("ok", "review"):
            unk_raw = "ok"
        decision = DecisionSettings(
            min_confident_match=float(d.get("min_confident_match", 0.6)),
            unknown_face_status=unk_raw,  # type: ignore[arg-type]
        )

        ex = raw.get("export") or {}
        flagged_raw = str(ex.get("flagged", "off")).lower()
        if flagged_raw not in ("off", "copy", "move"):
            flagged_raw = "off"
        statuses_raw = ex.get("flagged_status", ["blocked", "review"])
        if not isinstance(statuses_raw, list):
            statuses_raw = ["blocked", "review"]
        export = ExportSettings(
            flagged=flagged_raw,  # type: ignore[arg-type]
            flagged_status=tuple(str(x) for x in statuses_raw),
        )

        ing = raw.get("ingest") or {}
        ingest_root_raw = str(ing.get("destination_root") or "").strip()
        order_raw = str(ing.get("order") or "copy_then_analyze").strip().lower()
        ingest_order: IngestOrder = (
            "analyze_then_copy"
            if order_raw in ("analyze_then_copy", "analyze-then-copy")
            else "copy_then_analyze"
        )
        ingest = IngestSettings(
            enabled=bool(ing.get("enabled", False)),
            destination_root=(
                Path(os.path.expandvars(ingest_root_raw)).expanduser().resolve()
                if ingest_root_raw
                else None
            ),
            order=ingest_order,
        )

        col = raw.get("collect") or {}
        collect_root_raw = str(col.get("people_root") or "").strip()
        aspect_raw = str(col.get("crop_aspect") or "3:4").strip()
        aspect_w, aspect_h = 3.0, 4.0
        if ":" in aspect_raw:
            aw, ah = aspect_raw.split(":", 1)
            try:
                aspect_w = float(aw.strip())
                aspect_h = float(ah.strip())
            except ValueError:
                pass
        collect_thresh = col.get("match_threshold_collect")
        if collect_thresh is None:
            # Default: a bit above review so collect tracks flagged-ish matches.
            collect_thresh = float(
                (raw.get("matching") or {}).get("match_threshold_review", 200)
            ) + 40.0
        collect = CollectSettings(
            people_root=(
                Path(os.path.expandvars(collect_root_raw)).expanduser().resolve()
                if collect_root_raw
                else None
            ),
            crop_portrait=bool(col.get("crop_portrait", False)),
            crop_aspect_w=aspect_w,
            crop_aspect_h=aspect_h if aspect_h > 0 else 4.0,
            crop_padding=float(col.get("crop_padding", 1.5)),
            output_format="jpg",
            match_threshold_collect=float(collect_thresh),
        )

        lr_raw = raw.get("lightroom") or {}
        cl_default = {"blocked": "red", "review": "purple", "ok": "none"}
        cl_in = lr_raw.get("color_labels")
        if isinstance(cl_in, dict) and cl_in:
            lr_color_labels = {str(k).lower(): str(v) for k, v in cl_in.items()}
        else:
            lr_color_labels = dict(cl_default)
        xmp_in = lr_raw.get("xmp_label_values")
        if isinstance(xmp_in, dict) and xmp_in:
            xmp_label_values = {str(k).lower(): str(v) for k, v in xmp_in.items()}
        else:
            xmp_label_values = {}
        lightroom = LightroomSettings(
            enable=bool(lr_raw.get("enable", True)),
            color_labels=lr_color_labels,
            xmp_label_values=xmp_label_values,
        )

        md = raw.get("metadata") or {}
        writer_raw = str(md.get("writer", "")).strip().lower()
        if writer_raw in ("exiftool", "xmp_manual"):
            writer: Literal["exiftool", "xmp_manual"] = writer_raw  # type: ignore[assignment]
        else:
            mode_hint = str(md.get("mode", "")).strip().lower()
            writer = "xmp_manual" if mode_hint == "xmp_sidecar" else "exiftool"

        mode_raw = str(md.get("mode", "")).strip().lower()
        if writer == "xmp_manual":
            if mode_raw not in ("xmp_sidecar", "jpeg_embed"):
                mode_raw = "xmp_sidecar"
        elif mode_raw not in ("sidecar_for_raw", "jpeg_embed"):
            mode_raw = "sidecar_for_raw"

        if writer == "exiftool" and mode_raw == "xmp_sidecar":
            mode_raw = "sidecar_for_raw"
        if writer == "xmp_manual" and mode_raw == "sidecar_for_raw":
            mode_raw = "xmp_sidecar"

        def _parse_metadata_color_specs(
            meta_block: dict[str, Any], lr_cl: dict[str, str]
        ) -> dict[str, MetadataColorLabelSpec]:
            block = meta_block.get("color_labels")
            if isinstance(block, dict) and block:
                out: dict[str, MetadataColorLabelSpec] = {}
                for status, spec in block.items():
                    st = str(status).lower()
                    if isinstance(spec, dict):
                        xl = spec.get("xmp_label")
                        ps = spec.get("photoshop_label_color")
                        if xl is not None:
                            xs = str(xl).strip()
                            xl = None if xs.lower() in ("", "none", "null", "~") else xs
                        if ps is not None:
                            pss = str(ps).strip()
                            ps = None if pss.lower() in ("", "none", "null", "~") else pss.lower()
                        out[st] = MetadataColorLabelSpec(xmp_label=xl, photoshop_label_color=ps)
                    else:
                        raw = str(spec).strip()
                        if raw.lower() in ("none", "", "null", "~"):
                            out[st] = MetadataColorLabelSpec(None, None)
                        else:
                            out[st] = MetadataColorLabelSpec(raw.title(), raw.lower())
                return out
            out2: dict[str, MetadataColorLabelSpec] = {}
            for status, yaml_val in lr_cl.items():
                st = status.lower()
                v = str(yaml_val).strip()
                if v.lower() in ("none", "", "null", "~"):
                    out2[st] = MetadataColorLabelSpec(None, None)
                else:
                    out2[st] = MetadataColorLabelSpec(v.title(), v.lower())
            return out2

        rating_raw = md.get("xmp_rating_by_status")
        if isinstance(rating_raw, dict) and rating_raw:
            xmp_rating_by_status: dict[str, int] = {
                str(k).lower(): int(v) for k, v in rating_raw.items()
            }
        else:
            xmp_rating_by_status = {}

        ert_raw = str(md.get("exiftool_raw_target", "embedded")).strip().lower()
        if ert_raw not in ("embedded", "sidecar"):
            ert_raw = "embedded"
        exiftool_raw_target: Literal["embedded", "sidecar"] = ert_raw  # type: ignore[assignment]

        metadata = MetadataIntegrationSettings(
            enabled=bool(md.get("enabled", False)),
            writer=writer,
            mode=mode_raw,  # type: ignore[arg-type]
            exiftool_path=str(md.get("exiftool_path", "exiftool")).strip() or "exiftool",
            exiftool_timeout_sec=float(md.get("exiftool_timeout_sec", 120)),
            dry_run=bool(md.get("dry_run", False)),
            write_keywords=bool(md.get("write_keywords", True)),
            write_fields=bool(md.get("write_fields", True)),
            write_photoshop_label_color=bool(md.get("write_photoshop_label_color", True)),
            write_color_label=bool(md.get("write_color_label", True)),
            overwrite_color_labels=bool(md.get("overwrite_color_labels", False)),
            write_for_jpeg=bool(md.get("write_for_jpeg", False)),
            write_for_tiff=bool(md.get("write_for_tiff", False)),
            preserve_unrelated_keywords=bool(md.get("preserve_unrelated_keywords", True)),
            color_labels=_parse_metadata_color_specs(md, lr_color_labels),
            write_rating=bool(md.get("write_rating", False)),
            overwrite_ratings=bool(md.get("overwrite_ratings", False)),
            xmp_rating_by_status=xmp_rating_by_status,
            exiftool_raw_target=exiftool_raw_target,
            exiftool_preserve_xmpmm_document_id=bool(
                md.get("exiftool_preserve_xmpmm_document_id", True)
            ),
            exiftool_preserve_xmpmm_instance_id=bool(
                md.get("exiftool_preserve_xmpmm_instance_id", False)
            ),
            exiftool_config_path=(
                Path(p).expanduser().resolve()
                if (p := (md.get("exiftool_config_path") or "").strip())
                else None
            ),
            exiftool_verify_after_write=bool(md.get("exiftool_verify_after_write", False)),
        )

        logging_cfg = LoggingSettings(level=raw["logging"]["level"], audit_log_path=audit_path)

        usage_map = {str(k): str(v) for k, v in raw.get("usage_map", {}).items()}

        return Settings(
            database_url=database_url,
            insightface_root=insightface_root,
            logging=logging_cfg,
            pipeline=pipeline,
            matching=matching,
            decision=decision,
            export=export,
            ingest=ingest,
            collect=collect,
            metadata=metadata,
            lightroom=lightroom,
            usage_map=usage_map,
        )


def load_settings(config_path: Path | None = None) -> Settings:
    return Settings.from_dict(load_raw_config(config_path))
