"""Copy strong-matched photos into ``<people_root>/<person_name>/`` for later re-registration.

Runs as a batch step after analysis (alongside optional flagged export), querying the
database for strong face matches under the scan root. Analyze never re-registers or
writes embeddings; this only stages richer reference photos next to the existing
``people/<name>/`` folders so a manual ``register_person`` run can pick them up.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
from collections import defaultdict
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from faceit_ai.logging_setup import PHASE_CHECK, log_collect_audit, log_run_phase
from faceit_ai.persistence.models import Asset, AssetFace, Person
from faceit_ai.settings import CollectSettings, ImagePipelineSettings
from faceit_ai.vision.face_crop import (
    PortraitCropParams,
    crop_bgr_to_portrait,
    parse_bbox_json,
    write_portrait_jpeg,
)
from faceit_ai.vision.image_loader import ImageDecodeError, load_image_for_pipeline


def _short_sha8(path: Path) -> str:
    """First 8 hex chars of the file's SHA-256 (used to disambiguate name collisions)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()[:8]


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _crop_params_from_collect(collect: CollectSettings) -> PortraitCropParams:
    return PortraitCropParams(
        aspect_w=collect.crop_aspect_w,
        aspect_h=collect.crop_aspect_h,
        padding=collect.crop_padding,
    )


def _write_cropped_portrait(
    *,
    source_path: Path,
    dest: Path,
    bbox: tuple[float, float, float, float],
    image_cfg: ImagePipelineSettings,
    collect: CollectSettings,
    log: logging.Logger,
) -> bool:
    """Decode, crop, write JPEG. Returns True on success."""
    try:
        loaded = load_image_for_pipeline(source_path, image_cfg)
        cropped = crop_bgr_to_portrait(loaded.bgr, bbox, _crop_params_from_collect(collect))
        write_portrait_jpeg(dest, cropped)
        return True
    except (ImageDecodeError, OSError, ValueError, json.JSONDecodeError) as e:
        log.warning("collect: crop failed for %s: %s", source_path, e)
        return False


def collect_asset_for_person(
    *,
    source_path: Path,
    person_name: str,
    bbox: tuple[float, float, float, float] | None,
    people_root: Path,
    collect: CollectSettings,
    image_cfg: ImagePipelineSettings | None,
    audit: logging.Logger | None = None,
    logger: logging.Logger | None = None,
    overwrite: bool = False,
) -> int:
    """Copy or crop ``source_path`` into ``<people_root>/<person_name>/``.

    Returns 1 if a file was written, 0 otherwise.
    """
    log = logger or logging.getLogger("faceit_ai")
    name = person_name.strip()
    if not name:
        return 0

    src = source_path.expanduser()
    try:
        src_res = src.resolve()
    except OSError:
        log.warning("collect: cannot resolve source %s", src)
        return 0
    if not src_res.is_file():
        log.warning("collect: source missing on disk %s", src_res)
        return 0

    root_res = people_root.expanduser().resolve()
    if _is_under(src_res, root_res):
        return 0

    dest_dir = (root_res / name).resolve()
    dest_dir.mkdir(parents=True, exist_ok=True)

    use_crop = bool(collect.crop_portrait and bbox is not None and image_cfg is not None)
    if use_crop:
        dest = dest_dir / f"{src_res.stem}.jpg"
    else:
        dest = dest_dir / src_res.name

    if dest.exists() and not overwrite:
        try:
            if use_crop:
                # Re-crop when the source is newer (e.g. force re-analyze refreshed bboxes).
                same_size = dest.stat().st_mtime >= src_res.stat().st_mtime
            else:
                same_size = dest.stat().st_size == src_res.stat().st_size
        except OSError:
            same_size = False
        if same_size:
            if audit is not None:
                log_collect_audit(
                    audit,
                    src=str(src_res),
                    dest=str(dest),
                    person=name,
                    action="skip_identical",
                    extra={"reason": "destination_exists"},
                )
            return 0
        if not use_crop:
            dest = dest_dir / f"{src_res.stem}_{_short_sha8(src_res)}{src_res.suffix}"
            if dest.exists():
                if audit is not None:
                    log_collect_audit(
                        audit,
                        src=str(src_res),
                        dest=str(dest),
                        person=name,
                        action="skip_identical",
                        extra={"reason": "hashed_destination_exists"},
                    )
                return 0

    action = "copy"
    try:
        if use_crop and bbox is not None and image_cfg is not None:
            if _write_cropped_portrait(
                source_path=src_res,
                dest=dest,
                bbox=bbox,
                image_cfg=image_cfg,
                collect=collect,
                log=log,
            ):
                action = "copy_cropped"
            else:
                action = "copy_fallback"
                shutil.copy2(src_res, dest_dir / src_res.name)
                dest = dest_dir / src_res.name
        else:
            shutil.copy2(src_res, dest)
        if audit is not None:
            log_collect_audit(
                audit,
                src=str(src_res),
                dest=str(dest),
                person=name,
                action=action,
            )
        return 1
    except OSError as e:
        log.warning("collect: write failed %s -> %s: %s", src_res, dest, e)
        return 0


def list_strong_match_collect_jobs(
    session: Session,
    scan_root: Path,
    *,
    match_threshold: float,
) -> list[tuple[Path, dict[str, tuple[float, float, float, float] | None]]]:
    """``(resolved_asset_path, {person_name: bbox or None})`` under ``scan_root``."""
    root_res = scan_root.resolve()
    stmt = (
        select(Asset.path, Person.name, AssetFace.bbox, AssetFace.match_score)
        .join(AssetFace, AssetFace.asset_id == Asset.id)
        .join(Person, Person.id == AssetFace.match_person_id)
        .where(
            AssetFace.match_person_id.is_not(None),
            AssetFace.match_score.is_not(None),
            AssetFace.match_score >= float(match_threshold),
            Person.active.is_(True),
        )
    )
    # path -> person -> (score, bbox_json)
    best: dict[Path, dict[str, tuple[float, str]]] = defaultdict(dict)
    for path_str, person_name, bbox_json, score in session.execute(stmt):
        if not person_name or not str(person_name).strip():
            continue
        pname = str(person_name).strip()
        p = Path(path_str).expanduser()
        try:
            pr = p.resolve()
        except OSError:
            continue
        try:
            pr.relative_to(root_res)
        except ValueError:
            continue
        sc = float(score)
        prev = best[pr].get(pname)
        if prev is None or sc > prev[0]:
            best[pr][pname] = (sc, str(bbox_json))

    out: list[tuple[Path, dict[str, tuple[float, float, float, float] | None]]] = []
    for path, persons in best.items():
        bbox_map: dict[str, tuple[float, float, float, float] | None] = {}
        for pname, (_sc, bbox_json) in persons.items():
            try:
                bbox_map[pname] = parse_bbox_json(bbox_json)
            except (ValueError, json.JSONDecodeError):
                bbox_map[pname] = None
        if bbox_map:
            out.append((path, bbox_map))
    out.sort(key=lambda t: str(t[0]))
    return out


def collect_strong_matches_under_folder(
    *,
    session: Session,
    scan_root: Path,
    people_root: Path,
    match_threshold: float | None = None,
    match_threshold_strong: float | None = None,
    collect: CollectSettings | None = None,
    image_cfg: ImagePipelineSettings | None = None,
    audit: logging.Logger | None = None,
    logger: logging.Logger | None = None,
    overwrite: bool = False,
) -> tuple[int, int, int, list[str]]:
    """Copy or crop assets with match score >= threshold into ``<people_root>/<person>/``.

    Returns ``(n_assets, n_copies, n_missing, warnings)``.
    """
    log = logger or logging.getLogger("faceit_ai")
    eff_collect = collect or CollectSettings(people_root=people_root)
    # Prefer explicit match_threshold; keep match_threshold_strong as deprecated alias.
    thresh = (
        float(match_threshold)
        if match_threshold is not None
        else float(
            match_threshold_strong
            if match_threshold_strong is not None
            else eff_collect.match_threshold_collect
        )
    )
    jobs = list_strong_match_collect_jobs(
        session,
        scan_root,
        match_threshold=thresh,
    )
    if eff_collect.crop_portrait:
        log_run_phase(
            log,
            PHASE_CHECK,
            "collect — cropping portraits (%.0f:%.0f) — %d asset(s) score>=%.0f under %s → %s/<person>/",
            eff_collect.crop_aspect_w,
            eff_collect.crop_aspect_h,
            len(jobs),
            thresh,
            scan_root.resolve(),
            people_root.expanduser().resolve(),
        )
    else:
        log_run_phase(
            log,
            PHASE_CHECK,
            "collect — %d asset(s) score>=%.0f under %s → %s/<person>/",
            len(jobs),
            thresh,
            scan_root.resolve(),
            people_root.expanduser().resolve(),
        )

    n_assets = 0
    n_copies = 0
    n_missing = 0
    warnings: list[str] = []

    for source_path, person_bboxes in jobs:
        if not source_path.is_file():
            n_missing += 1
            warnings.append(f"missing on disk: {source_path}")
            continue
        n_assets += 1
        for person_name, bbox in person_bboxes.items():
            n_copies += collect_asset_for_person(
                source_path=source_path,
                person_name=person_name,
                bbox=bbox,
                people_root=people_root,
                collect=eff_collect,
                image_cfg=image_cfg,
                audit=audit,
                logger=log,
                overwrite=overwrite,
            )

    if warnings:
        for w in warnings[:50]:
            log.warning("%s", w)
        if len(warnings) > 50:
            log.warning("… %d more collect warnings omitted", len(warnings) - 50)

    log.info(
        "collect — done | assets=%d copies=%d missing=%d score>=%.0f dest=%s",
        n_assets,
        n_copies,
        n_missing,
        thresh,
        people_root.expanduser().resolve(),
    )
    return n_assets, n_copies, n_missing, warnings
