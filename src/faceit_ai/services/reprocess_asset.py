"""Re-analyze a single asset (used from Review when face assignments change)."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from faceit_ai.decision.engine import FaceDecisionInput, decide_image
from faceit_ai.integration.metadata_port import MetadataSyncPort, MetadataWriteRequest
from faceit_ai.persistence.models import Asset, AssetDecision, Consent
from faceit_ai.persistence.repository import AssetRepository, ConsentRepository
from faceit_ai.persistence.session import session_scope
from faceit_ai.services.analyze_photos import _load_consent_map
from faceit_ai.services.flagged_export import (
    export_single_flagged_asset,
    prune_stale_flagged_exports,
)
from faceit_ai.settings import Settings
from faceit_ai.vision.image_loader import ImageDecodeError, file_digest_sha256, load_image_for_pipeline
from faceit_ai.vision.insightface_backend import InsightFaceBackend
from faceit_ai.vision.matcher import MatchResult, match_embedding


def bbox_iou(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
) -> float:
    x1 = max(a[0], b[0])
    y1 = max(a[1], b[1])
    x2 = min(a[2], b[2])
    y2 = min(a[3], b[3])
    inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    if inter <= 0:
        return 0.0
    area_a = max(1.0, (a[2] - a[0]) * (a[3] - a[1]))
    area_b = max(1.0, (b[2] - b[0]) * (b[3] - b[1]))
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def _resolve_analyze_path(asset_path: Path, folder: Path) -> Path | None:
    """Prefer original source outside ``flagged/`` when the DB path is under flagged."""
    from faceit_ai.services.flagged_export import _resolve_source_file, _restore_path_outside_flagged

    root_res = folder.resolve()
    try:
        resolved = asset_path.expanduser().resolve()
    except OSError:
        return None
    if resolved.is_file():
        restore = _restore_path_outside_flagged(resolved, root_res)
        if restore is not None and restore.is_file() and restore != resolved:
            return restore
        return resolved
    alt = _resolve_source_file(resolved, root_res)
    return alt


@dataclass(frozen=True)
class ReprocessAssetResult:
    asset_id: int
    status: str
    reason: str
    metadata_applied: bool
    flagged_pruned: int


def reprocess_single_asset(
    *,
    asset_id: int,
    folder: Path,
    settings: Settings,
    session_factory: sessionmaker[Any],
    backend: InsightFaceBackend,
    metadata: MetadataSyncPort | None = None,
    preserve_unknown_bboxes: list[tuple[float, float, float, float]] | None = None,
    export_flagged: Literal["off", "copy", "move"] = "off",
    audit: logging.Logger | None = None,
    logger: logging.Logger | None = None,
    usage: str | None = None,
) -> ReprocessAssetResult:
    """Full detect + match + decide for one asset; optional unknown-bbox preservation."""
    log = logger or logging.getLogger("faceit_ai")
    preserve = list(preserve_unknown_bboxes or [])
    iou_threshold = 0.3

    with session_scope(session_factory) as session:
        asset = session.get(Asset, asset_id)
        if asset is None:
            raise ValueError(f"Unknown asset id {asset_id}")
        decision = session.scalar(select(AssetDecision).where(AssetDecision.asset_id == asset_id))
        if decision is None:
            raise ValueError(f"Missing decision for asset {asset_id}")
        usage_key = usage or str(decision.usage)
        if usage_key not in settings.usage_map:
            raise ValueError(f"Unknown usage {usage_key!r}")
        usage_column = settings.usage_map[usage_key]

        source = _resolve_analyze_path(Path(asset.path), folder)
        if source is None or not source.is_file():
            raise ValueError("Source file missing on disk")

        sha = file_digest_sha256(source)
        try:
            loaded = load_image_for_pipeline(source, settings.pipeline.image)
        except ImageDecodeError as err:
            raise ValueError(f"Unreadable source: {err}") from err

        gallery = ConsentRepository(session).list_all_embeddings(backend.embedding_dim)
        consent_by_person = _load_consent_map(session)

        faces = backend.analyze(loaded.bgr)
        face_inputs: list[FaceDecisionInput] = []
        rows_for_db: list[tuple[str, Any, int | None, float | None]] = []

        for fd in faces:
            mr = match_embedding(fd.embedding, gallery, settings.matching)
            bbox = fd.bbox_xyxy
            for unk in preserve:
                if bbox_iou(bbox, unk) >= iou_threshold:
                    mr = MatchResult(
                        person_id=None,
                        person_name=None,
                        score=mr.score,
                        is_unknown=True,
                        needs_review_uncertain=False,
                    )
                    break
            face_inputs.append(FaceDecisionInput(match=mr))
            bbox_json = json.dumps([round(x, 2) for x in bbox])
            rows_for_db.append((bbox_json, fd.embedding, mr.person_id, mr.score))

        agg = decide_image(
            face_inputs=face_inputs,
            consent_lookup=consent_by_person,
            usage_column=usage_column,
            decision_cfg=settings.decision,
        )

        assets = AssetRepository(session)
        assets.mark_processed(
            path=str(source),
            sha256=sha,
            faces=rows_for_db,
            decision_status=agg.status,
            decision_reason=agg.reason,
            usage=usage_key,
        )
        dec = session.scalar(select(AssetDecision).where(AssetDecision.asset_id == asset_id))
        if dec is not None:
            dec.manual_override = False

        final_status = agg.status
        final_reason = agg.reason

    metadata_applied = False
    if metadata is not None and settings.metadata.enabled:
        try:
            identified = sum(1 for f in agg.faces_out if f.get("person") is not None)
            confs = [f.get("confidence") for f in agg.faces_out if f.get("confidence") is not None]
            conf_max = max(confs) if confs else None
            metadata.apply(
                MetadataWriteRequest(
                    file_path=str(source),
                    status=final_status,
                    reason=final_reason,
                    usage=usage_key,
                    face_count=len(faces),
                    faces_identified=identified,
                    match_confidence_max=conf_max,
                )
            )
            metadata_applied = True
        except Exception:
            log.exception("reprocess_single_asset: metadata failed for %s", source)

    n_pruned = 0
    if export_flagged in ("copy", "move"):
        with session_scope(session_factory) as session:
            removed, restored, _ = prune_stale_flagged_exports(
                session=session,
                scan_root=folder,
                action=export_flagged,
                audit=audit,
                logger=log,
            )
            n_pruned = removed + restored
            if final_status in ("blocked", "review"):
                export_single_flagged_asset(
                    session=session,
                    scan_root=folder,
                    source_path=source,
                    decision_status=final_status,  # type: ignore[arg-type]
                    action=export_flagged,
                    audit=audit,
                    logger=log,
                )

    return ReprocessAssetResult(
        asset_id=asset_id,
        status=final_status,
        reason=final_reason,
        metadata_applied=metadata_applied,
        flagged_pruned=n_pruned,
    )
