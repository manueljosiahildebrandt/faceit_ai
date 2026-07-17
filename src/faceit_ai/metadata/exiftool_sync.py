"""Write Lightroom-oriented XMP using ExifTool (embedded in RAW by default; optional ``.xmp`` sidecar)."""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from faceit_ai.integration.metadata_port import MetadataWriteRequest
from faceit_ai.logging_setup import log_metadata_sync
from faceit_ai.metadata.cleanup import (
    tool_owned_hierarchical_from_list,
    tool_owned_plain_from_list,
)
from faceit_ai.metadata.keyword_builder import build_metadata_payload
from faceit_ai.settings import MetadataIntegrationSettings, Settings


def _as_str_list(val: Any) -> list[str]:
    if val is None:
        return []
    if isinstance(val, str):
        return [val] if val.strip() else []
    if isinstance(val, list):
        out: list[str] = []
        for x in val:
            if isinstance(x, dict) and "val" in x:
                s = str(x["val"]).strip()
            else:
                s = str(x).strip()
            if s:
                out.append(s)
        return out
    s = str(val).strip()
    return [s] if s else []


def _resolve_exiftool_bin(meta: MetadataIntegrationSettings) -> str | None:
    p = meta.exiftool_path.strip()
    if not p:
        return None
    path = Path(p)
    if path.is_file():
        return str(path.resolve())
    return shutil.which(p)


def _write_strategy(
    path: Path, settings: Settings
) -> tuple[Literal["skip", "sidecar_raw", "embedded"], str]:
    """Return (strategy, human-readable mode label for logs/audit)."""
    meta = settings.metadata
    suf = path.suffix.lower()
    raws = set(settings.pipeline.image.raw_extensions)

    if suf in raws:
        if meta.exiftool_raw_target == "embedded":
            return "embedded", "embedded_in_raw"
        return "sidecar_raw", "sidecar_for_raw"

    if meta.mode == "jpeg_embed":
        if suf in (".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff"):
            return "embedded", "jpeg_embed"
        return "skip", "jpeg_embed_unsupported"

    # sidecar_for_raw: only optional raster embed
    if suf in (".jpg", ".jpeg") and meta.write_for_jpeg:
        return "embedded", "embedded_jpeg"
    if suf in (".tif", ".tiff") and meta.write_for_tiff:
        return "embedded", "embedded_tiff"

    return "skip", "sidecar_for_raw_non_raw"


def _sidecar_stem(raw_path: Path) -> str:
    """Drop a trailing ``_original`` (any case) from the RAW basename so the sidecar is ``foo.xmp``."""
    s = raw_path.stem
    low = s.lower()
    if low.endswith("_original") and len(s) > len("_original"):
        return s[: -len("_original")]
    return s


def _canonical_xmp_sidecar(raw_path: Path) -> Path:
    """Lightroom-style sidecar next to the RAW: ``{sidecar_stem}.xmp`` (``stem`` without ``_original``)."""
    return raw_path.parent / f"{_sidecar_stem(raw_path)}.xmp"


ExifToolTaggingKind = Literal[
    "embedded",
    "sidecar_inplace",
    "sidecar_new_from_raw",
    "sidecar_migrate_from_legacy",
]


@dataclass(frozen=True)
class ExifToolWritePlan:
    """How ExifTool reads XMP state and which file(s) it updates."""

    read_target: Path
    exiftool_source: Path
    output_sidecar: Path | None
    tagging_kind: ExifToolTaggingKind


def _exiftool_xmp_write_plan(
    raw_path: Path, strategy: Literal["sidecar_raw", "embedded"]
) -> ExifToolWritePlan:
    """
    Build read/write targets. Prefer the **canonical** ``{sidecar_stem}.xmp`` so merges keep LR’s XMP
    and new sidecars are not named ``foo_original.xmp``. When only a legacy ``{raw_stem}.xmp`` exists,
    read it and write **-o** the canonical path.
    """
    if strategy == "embedded":
        return ExifToolWritePlan(
            read_target=raw_path,
            exiftool_source=raw_path,
            output_sidecar=None,
            tagging_kind="embedded",
        )
    canonical = _canonical_xmp_sidecar(raw_path)
    adjacent_legacy = raw_path.parent / f"{raw_path.stem}.xmp"

    if canonical.is_file():
        return ExifToolWritePlan(
            read_target=canonical,
            exiftool_source=canonical,
            output_sidecar=None,
            tagging_kind="sidecar_inplace",
        )
    if adjacent_legacy.is_file() and adjacent_legacy.resolve() != canonical.resolve():
        return ExifToolWritePlan(
            read_target=adjacent_legacy,
            exiftool_source=adjacent_legacy,
            output_sidecar=canonical,
            tagging_kind="sidecar_migrate_from_legacy",
        )
    return ExifToolWritePlan(
        read_target=raw_path,
        exiftool_source=raw_path,
        output_sidecar=canonical,
        tagging_kind="sidecar_new_from_raw",
    )


def _verify_xmp_read_path(
    asset_path: Path, strategy: Literal["sidecar_raw", "embedded"]
) -> Path:
    """Path for verify read-back: the media file, or canonical ``.xmp`` when using a sidecar."""
    if strategy == "embedded":
        return asset_path
    return _canonical_xmp_sidecar(asset_path)


def _read_xmp_tags(exiftool: str, path: Path, timeout: float) -> dict[str, Any]:
    cmd = [
        exiftool,
        "-j",
        "-XMP:Label",
        "-Photoshop:LabelColor",
        "-XMP:Rating",
        "-XMP-dc:Subject",
        "-XMP-lr:HierarchicalSubject",
        "-XMP-xmpMM:DocumentID",
        "-XMP-xmpMM:OriginalDocumentID",
        "-XMP-xmpMM:InstanceID",
        str(path),
    ]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    if proc.returncode != 0:
        return {}
    try:
        data = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        return {}
    if not data or not isinstance(data, list):
        return {}
    row = data[0]
    return row if isinstance(row, dict) else {}


def _verify_after_write(exiftool: str, path: Path, timeout: float) -> dict[str, Any]:
    """Read back standard Lightroom-oriented tags after a write (audit + verify_ok)."""
    cmd = [
        exiftool,
        "-j",
        "-a",
        "-XMP:Label",
        "-Photoshop:LabelColor",
        "-XMP:Rating",
        "-XMP-dc:Subject",
        "-XMP-lr:HierarchicalSubject",
        "-XMP-xmpMM:DocumentID",
        "-XMP-xmpMM:InstanceID",
        str(path),
    ]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    out = (proc.stdout or "").strip()
    row: dict[str, Any] = {}
    if proc.returncode == 0 and out:
        try:
            data = json.loads(out)
            if isinstance(data, list) and data and isinstance(data[0], dict):
                row = data[0]
        except json.JSONDecodeError:
            pass
    subj = _as_str_list(row.get("Subject"))
    hier = _as_str_list(row.get("HierarchicalSubject"))
    vd, _, vi = _xmpmm_ids_from_row(row)
    return {
        "exit_code": proc.returncode,
        "stdout_preview": out[:1500] if out else None,
        "stderr_preview": ((proc.stderr or "").strip()[:1500] if proc.stderr else None),
        "xmp_label": _first_nonempty_str(row, "Label", "XMP:Label"),
        "photoshop_label_color": _first_nonempty_str(
            row, "Label Color", "LabelColor", "Photoshop:LabelColor"
        ),
        "rating": _parse_rating_value(row.get("Rating")),
        "subject_keywords": subj,
        "hierarchical_keywords": hier,
        "xmpmm_document_id": vd,
        "xmpmm_instance_id": vi,
    }


def _parse_rating_value(val: Any) -> int | None:
    """ExifTool JSON may return Rating as int, float, or string (Lightroom uses 0–5)."""
    if val is None:
        return None
    try:
        if isinstance(val, str) and not val.strip():
            return None
        r = int(round(float(val)))
        return max(0, min(5, r))
    except (TypeError, ValueError):
        return None


def _rating_from_row(row: dict[str, Any]) -> int | None:
    return _parse_rating_value(row.get("Rating"))


def _first_nonempty_str(row: dict[str, Any], *keys: str) -> str | None:
    for k in keys:
        v = row.get(k)
        if v is None:
            continue
        s = str(v).strip()
        if s:
            return s
    return None


def _xmpmm_ids_from_row(row: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    """(DocumentID, OriginalDocumentID, InstanceID) — ExifTool JSON uses short or group-prefixed keys."""
    doc = _first_nonempty_str(row, "XMP-xmpMM:DocumentID", "DocumentID")
    orig = _first_nonempty_str(row, "XMP-xmpMM:OriginalDocumentID", "OriginalDocumentID")
    inst = _first_nonempty_str(row, "XMP-xmpMM:InstanceID", "InstanceID")
    return doc, orig, inst


def _should_skip_label_write(
    *,
    overwrite_color_labels: bool,
    write_photoshop_label_color: bool,
    payload: Any,
    existing_label: str | None,
    existing_ps_color: str | None,
) -> bool:
    """When overwrite is false, skip only if disk already matches what we would write."""
    if overwrite_color_labels:
        return False
    if payload.clear_color_labels:
        if (existing_label or "").strip():
            return False
        if write_photoshop_label_color and (existing_ps_color or "").strip():
            return False
        return True
    wants_ps = write_photoshop_label_color and bool(payload.photoshop_label_color)
    if not (payload.xmp_label or wants_ps):
        return True
    ps_t = (payload.photoshop_label_color or "").strip().lower()
    xmp_t = (payload.xmp_label or "").strip()
    ps_e = (existing_ps_color or "").strip().lower()
    xmp_e = (existing_label or "").strip()
    if payload.xmp_label and xmp_e != xmp_t:
        return False
    if wants_ps and ps_e != ps_t:
        return False
    return True


def _should_skip_rating_write(
    *,
    overwrite_ratings: bool,
    target: int,
    existing: int | None,
) -> bool:
    if overwrite_ratings:
        return False
    ex = 0 if existing is None else existing
    return ex == target


def _build_write_args(
    *,
    exiftool: str,
    plan: ExifToolWritePlan,
    strategy: Literal["sidecar_raw", "embedded"],
    meta: MetadataIntegrationSettings,
    payload: Any,
    existing_label: str | None,
    existing_ps_color: str | None,
    existing_rating: int | None,
    subjects: list[str],
    hierarchical: list[str],
    xmpmm_document_id: str | None = None,
    xmpmm_original_document_id: str | None = None,
    xmpmm_instance_id: str | None = None,
    lightroom_labels_enabled: bool = True,
) -> list[str]:
    # No -m: suppressing minor errors can hide failed tag writes.
    cmd: list[str] = [exiftool]
    if meta.exiftool_config_path is not None and meta.exiftool_config_path.is_file():
        cmd.extend(["-config", str(meta.exiftool_config_path)])
    if strategy == "embedded":
        cmd.append("-overwrite_original_in_place")
    elif plan.tagging_kind == "sidecar_inplace":
        cmd.append("-overwrite_original_in_place")
    elif plan.output_sidecar is not None:
        cmd.extend(["-o", str(plan.output_sidecar)])

    if meta.write_color_label and lightroom_labels_enabled:
        wants_ps = meta.write_photoshop_label_color and bool(payload.photoshop_label_color)
        if payload.clear_color_labels:
            cmd.append("-XMP:Label=")
            if meta.write_photoshop_label_color:
                cmd.append("-Photoshop:LabelColor=")
        elif payload.xmp_label or wants_ps:
            if not _should_skip_label_write(
                overwrite_color_labels=meta.overwrite_color_labels,
                write_photoshop_label_color=meta.write_photoshop_label_color,
                payload=payload,
                existing_label=existing_label,
                existing_ps_color=existing_ps_color,
            ):
                if payload.xmp_label:
                    cmd.append(f"-XMP:Label={payload.xmp_label}")
                if wants_ps:
                    cmd.append(f"-Photoshop:LabelColor={payload.photoshop_label_color}")

    if meta.write_rating and payload.xmp_rating is not None:
        if not _should_skip_rating_write(
            overwrite_ratings=meta.overwrite_ratings,
            target=payload.xmp_rating,
            existing=existing_rating,
        ):
            cmd.append(f"-XMP:Rating={payload.xmp_rating}")

    if meta.write_keywords:
        for v in tool_owned_plain_from_list(subjects):
            cmd.append(f"-XMP-dc:Subject-={v}")
        for v in tool_owned_hierarchical_from_list(hierarchical):
            cmd.append(f"-XMP-lr:HierarchicalSubject-={v}")
        for v in payload.plain_keywords:
            cmd.append(f"-XMP-dc:Subject+={v}")
        for v in payload.hierarchical_keywords:
            cmd.append(f"-XMP-lr:HierarchicalSubject+={v}")

    if meta.write_fields and meta.exiftool_config_path is not None and meta.exiftool_config_path.is_file():
        for k, val in payload.custom_fields.items():
            if val is None:
                continue
            tag = f"-XMP-sola:{k}={val}"
            cmd.append(tag)

    if meta.exiftool_preserve_xmpmm_document_id and xmpmm_document_id:
        cmd.append(f"-XMP-xmpMM:DocumentID={xmpmm_document_id}")
    if meta.exiftool_preserve_xmpmm_document_id and xmpmm_original_document_id:
        cmd.append(f"-XMP-xmpMM:OriginalDocumentID={xmpmm_original_document_id}")
    if meta.exiftool_preserve_xmpmm_instance_id and xmpmm_instance_id:
        cmd.append(f"-XMP-xmpMM:InstanceID={xmpmm_instance_id}")

    cmd.append(str(plan.exiftool_source))
    return cmd


class ExifToolMetadataSync:
    """ExifTool-backed writer; failures are logged, not raised."""

    def __init__(
        self,
        settings: Settings,
        *,
        log: logging.Logger | None = None,
        audit: logging.Logger | None = None,
    ) -> None:
        self._settings = settings
        self._meta = settings.metadata
        self._log = log or logging.getLogger("faceit_ai.metadata")
        self._audit = audit
        self._missing_binary_logged = False
        self._custom_fields_hint_logged = False

    def apply(self, req: MetadataWriteRequest) -> None:
        path = Path(req.file_path)
        strategy, mode_label = _write_strategy(path, self._settings)
        if strategy == "skip":
            self._log.debug("metadata exiftool skip %s (%s)", path, mode_label)
            log_metadata_sync(
                self._audit,
                asset_path=str(path),
                status=req.status,
                writer="exiftool",
                mode=mode_label,
                success=True,
                extra={"skipped": True, "reason": mode_label},
            )
            return

        exiftool = _resolve_exiftool_bin(self._meta)
        if exiftool is None:
            if not self._missing_binary_logged:
                self._log.warning(
                    "ExifTool not found (%r). Install it (e.g. `brew install exiftool`) or set "
                    "metadata.exiftool_path to the full binary (often /opt/homebrew/bin/exiftool "
                    "on Apple Silicon). Until then, metadata writes are skipped.",
                    self._meta.exiftool_path,
                )
                self._missing_binary_logged = True
            log_metadata_sync(
                self._audit,
                asset_path=str(path),
                status=req.status,
                writer="exiftool",
                mode=mode_label,
                success=False,
                extra={
                    "error": "exiftool_not_found",
                    "hint": "Install ExifTool (brew install exiftool) or set metadata.exiftool_path "
                    "to the full path of the exiftool binary.",
                },
            )
            return

        if self._meta.write_fields and self._meta.exiftool_config_path is None:
            if not self._custom_fields_hint_logged:
                self._log.info(
                    "metadata.write_fields is on but metadata.exiftool_config_path is unset: "
                    "ExifTool only knows standard XMP tags. Custom keys (gdpr_*, face counts) need a "
                    "separate ExifTool -config file—not the same as exiftool_path (the binary). "
                    "Keywords and color labels are still written. Set write_fields: false to silence this."
                )
                self._custom_fields_hint_logged = True

        payload = build_metadata_payload(req, self._meta, self._settings.lightroom)
        if strategy == "embedded":
            plan = ExifToolWritePlan(
                read_target=path,
                exiftool_source=path,
                output_sidecar=None,
                tagging_kind="embedded",
            )
            canonical_sidecar = None
        else:
            plan = _exiftool_xmp_write_plan(path, strategy)
            canonical_sidecar = _canonical_xmp_sidecar(path)
            if plan.tagging_kind == "sidecar_inplace":
                self._log.debug(
                    "metadata exiftool using existing sidecar %s (preserve LR XMP shape)",
                    plan.exiftool_source,
                )
            elif plan.tagging_kind == "sidecar_migrate_from_legacy":
                self._log.debug(
                    "metadata exiftool migrating XMP %s -> %s",
                    plan.exiftool_source,
                    plan.output_sidecar,
                )

        t0 = time.perf_counter()
        try:
            row = _read_xmp_tags(exiftool, plan.read_target, self._meta.exiftool_timeout_sec)
        except subprocess.TimeoutExpired:
            self._log.warning("ExifTool read timeout for %s", path)
            log_metadata_sync(
                self._audit,
                asset_path=str(path),
                status=req.status,
                writer="exiftool",
                mode=mode_label,
                success=False,
                extra={"error": "read_timeout"},
            )
            return
        except OSError as e:
            self._log.warning("ExifTool read failed for %s: %s", path, e)
            log_metadata_sync(
                self._audit,
                asset_path=str(path),
                status=req.status,
                writer="exiftool",
                mode=mode_label,
                success=False,
                extra={"error": str(e)},
            )
            return

        subjects = _as_str_list(row.get("Subject"))
        hierarchical = _as_str_list(row.get("HierarchicalSubject"))
        existing_label = _first_nonempty_str(row, "Label", "XMP:Label")
        existing_ps = _first_nonempty_str(row, "Label Color", "LabelColor", "Photoshop:LabelColor")
        existing_rating = _rating_from_row(row)
        pre_xmpmm_doc, pre_xmpmm_orig, pre_xmpmm_inst = _xmpmm_ids_from_row(row)

        cmd = _build_write_args(
            exiftool=exiftool,
            plan=plan,
            strategy=strategy,
            meta=self._meta,
            payload=payload,
            existing_label=existing_label,
            existing_ps_color=existing_ps,
            existing_rating=existing_rating,
            subjects=subjects,
            hierarchical=hierarchical,
            xmpmm_document_id=pre_xmpmm_doc,
            xmpmm_original_document_id=pre_xmpmm_orig,
            xmpmm_instance_id=pre_xmpmm_inst,
            lightroom_labels_enabled=self._settings.lightroom.enable,
        )

        if self._meta.dry_run:
            ms = (time.perf_counter() - t0) * 1000.0
            self._log.info("metadata exiftool dry_run cmd=%s", cmd)
            log_metadata_sync(
                self._audit,
                asset_path=str(path),
                status=req.status,
                writer="exiftool",
                mode=mode_label,
                success=True,
                extra={
                    "dry_run": True,
                    "command": cmd,
                    "plain_keywords": list(payload.plain_keywords),
                    "hierarchical_keywords": list(payload.hierarchical_keywords),
                    "xmp_label": payload.xmp_label,
                    "photoshop_label_color": payload.photoshop_label_color,
                    "requested_xmp_label": payload.xmp_label,
                    "xmp_rating": payload.xmp_rating,
                    "exiftool_tagging_target": str(plan.exiftool_source),
                    "exiftool_tagging_kind": plan.tagging_kind,
                    "exiftool_sidecar_canonical": str(canonical_sidecar)
                    if canonical_sidecar is not None
                    else None,
                    "duration_ms": round(ms, 2),
                },
            )
            return

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self._meta.exiftool_timeout_sec,
                check=False,
            )
        except subprocess.TimeoutExpired:
            self._log.warning("ExifTool write timeout for %s", path)
            log_metadata_sync(
                self._audit,
                asset_path=str(path),
                status=req.status,
                writer="exiftool",
                mode=mode_label,
                success=False,
                extra={"error": "write_timeout"},
            )
            return
        except OSError as e:
            self._log.warning("ExifTool write failed for %s: %s", path, e)
            log_metadata_sync(
                self._audit,
                asset_path=str(path),
                status=req.status,
                writer="exiftool",
                mode=mode_label,
                success=False,
                extra={"error": str(e)},
            )
            return

        ms = (time.perf_counter() - t0) * 1000.0
        ok = proc.returncode == 0
        if not ok:
            err = (proc.stderr or proc.stdout or "").strip()
            self._log.warning(
                "ExifTool exit %s for %s: %s",
                proc.returncode,
                path,
                err[:500] if err else "(no stderr)",
            )
        else:
            self._log.debug(
                "metadata exiftool ok %s labels=%s/%s keywords=%s",
                path,
                payload.xmp_label,
                payload.photoshop_label_color,
                len(payload.plain_keywords),
            )

        wrote_label_args = any(
            a.startswith("-XMP:Label")
            or (
                a.startswith("-Photoshop:LabelColor") and self._meta.write_photoshop_label_color
            )
            for a in cmd
        )
        wrote_rating_args = any(a.startswith("-XMP:Rating") for a in cmd)
        verify: dict[str, Any] | None = None
        verify_ok: bool | None = None
        if ok and self._meta.exiftool_verify_after_write:
            try:
                verify = _verify_after_write(
                    exiftool,
                    _verify_xmp_read_path(path, strategy),
                    self._meta.exiftool_timeout_sec,
                )
                exp_ps = (payload.photoshop_label_color or "").strip().lower()
                exp_xmp = (payload.xmp_label or "").strip()
                got_ps = (verify.get("photoshop_label_color") or "").strip().lower()
                got_xmp = (verify.get("xmp_label") or "").strip()
                if wrote_label_args:
                    if payload.clear_color_labels:
                        xmp_ok = not got_xmp
                        ps_ok = (not self._meta.write_photoshop_label_color) or (not got_ps)
                        verify_ok = xmp_ok and ps_ok
                    else:
                        xmp_match = (not exp_xmp) or got_xmp == exp_xmp
                        if self._meta.write_photoshop_label_color and exp_ps:
                            ps_match = got_ps == exp_ps
                        else:
                            ps_match = True
                        verify_ok = xmp_match and ps_match
                    if verify_ok is False:
                        self._log.warning(
                            "metadata was written but read-back verification failed (labels may still "
                            "have changed on disk; check with exiftool or Lightroom Read Metadata From File): "
                            "%s | expected xmp=%r ps=%r | got xmp=%r ps=%r",
                            path,
                            exp_xmp or None,
                            exp_ps or None,
                            got_xmp or None,
                            got_ps or None,
                        )
                if wrote_rating_args and verify is not None and payload.xmp_rating is not None:
                    got_n = verify.get("rating")
                    rating_ok = got_n == payload.xmp_rating
                    verify["rating_ok"] = rating_ok
                    if verify_ok is None:
                        verify_ok = rating_ok
                    elif verify_ok is not False:
                        verify_ok = verify_ok and rating_ok
                    if not rating_ok:
                        self._log.warning(
                            "metadata rating write verification failed (read-back mismatch after ExifTool): "
                            "%s | expected rating=%s | got=%s",
                            path,
                            payload.xmp_rating,
                            got_n,
                        )
                if self._meta.write_keywords and payload.plain_keywords and verify is not None:
                    subj = verify.get("subject_keywords") or []
                    hier = verify.get("hierarchical_keywords") or []
                    if isinstance(subj, list):
                        subj_set = {str(x).strip() for x in subj if str(x).strip()}
                    else:
                        subj_set = set()
                    if isinstance(hier, list):
                        hier_set = {str(x).strip() for x in hier if str(x).strip()}
                    else:
                        hier_set = set()
                    kw_plain_ok = all(k in subj_set for k in payload.plain_keywords)
                    kw_hier_ok = all(k in hier_set for k in payload.hierarchical_keywords)
                    verify["keyword_plain_ok"] = kw_plain_ok
                    verify["keyword_hierarchical_ok"] = kw_hier_ok
                    kw_ok = kw_plain_ok and kw_hier_ok
                    if verify_ok is None:
                        verify_ok = kw_ok
                    elif verify_ok is not False:
                        verify_ok = verify_ok and kw_ok
                if (
                    verify is not None
                    and self._meta.exiftool_preserve_xmpmm_document_id
                    and pre_xmpmm_doc
                ):
                    got_d = (verify.get("xmpmm_document_id") or "").strip()
                    doc_ok = got_d == pre_xmpmm_doc.strip()
                    verify["xmpmm_document_id_preserved"] = doc_ok
                    if not doc_ok:
                        self._log.warning(
                            "metadata xmpMM:DocumentID changed after write (LR document identity may drift): "
                            "%s | expected %r | got %r",
                            path,
                            pre_xmpmm_doc,
                            got_d or None,
                        )
                        if verify_ok is None:
                            verify_ok = False
                        elif verify_ok is not False:
                            verify_ok = False
                if (
                    verify is not None
                    and self._meta.exiftool_preserve_xmpmm_instance_id
                    and pre_xmpmm_inst
                ):
                    got_i = (verify.get("xmpmm_instance_id") or "").strip()
                    inst_ok = got_i == pre_xmpmm_inst.strip()
                    verify["xmpmm_instance_id_preserved"] = inst_ok
                    if not inst_ok:
                        self._log.warning(
                            "metadata xmpMM:InstanceID changed after write (expected if preserving instance): "
                            "%s | expected %r | got %r",
                            path,
                            pre_xmpmm_inst,
                            got_i or None,
                        )
                        if verify_ok is None:
                            verify_ok = False
                        elif verify_ok is not False:
                            verify_ok = False
            except subprocess.TimeoutExpired:
                verify = {"error": "verify_timeout"}
                verify_ok = False
            except OSError as e:
                verify = {"error": str(e)}
                verify_ok = False

        if strategy == "embedded":
            touch = "embedded"
        elif plan.tagging_kind == "sidecar_inplace":
            touch = "sidecar_inplace"
        elif plan.tagging_kind == "sidecar_new_from_raw":
            touch = "sidecar_out_from_raw"
        elif plan.tagging_kind == "sidecar_migrate_from_legacy":
            touch = "sidecar_out_from_legacy"
        else:
            touch = "sidecar_out_from_raw"
        extra: dict[str, Any] = {
            "exit_code": proc.returncode,
            "plain_keywords": list(payload.plain_keywords),
            "hierarchical_keywords": list(payload.hierarchical_keywords),
            "xmp_label": payload.xmp_label,
            "photoshop_label_color": payload.photoshop_label_color,
            "requested_xmp_label": payload.xmp_label,
            "requested_xmp_rating": payload.xmp_rating,
            "requested_status": req.status,
            "stderr": (proc.stderr or "")[:2000] if not ok else None,
            "duration_ms": round(ms, 2),
            "embedded": strategy == "embedded",
            "metadata_touch": touch,
            "exiftool_tagging_target": str(plan.exiftool_source),
            "exiftool_tagging_kind": plan.tagging_kind,
            "exiftool_sidecar_canonical": str(canonical_sidecar)
            if canonical_sidecar is not None
            else None,
            "verify": verify,
            "verify_ok": verify_ok,
        }
        if verify and ok:
            extra["actual_xmp_label"] = verify.get("xmp_label")
        log_metadata_sync(
            self._audit,
            asset_path=str(path),
            status=req.status,
            writer="exiftool",
            mode=mode_label,
            success=ok,
            extra=extra,
        )
