"""Review gallery: list review/blocked assets, confirm blocked or clear to OK."""

from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import cv2
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from faceit_ai.integration.metadata_port import MetadataSyncPort, MetadataWriteRequest
from faceit_ai.logging_setup import log_collect_audit
from faceit_ai.persistence.models import Asset, AssetDecision, AssetFace, Person, blob_to_embedding
from faceit_ai.persistence.repository import ConsentRepository
from faceit_ai.persistence.session import session_scope
from faceit_ai.services.collect_matches import _write_cropped_portrait
from faceit_ai.services.collected_photos import upsert_collected_photo
from faceit_ai.services.flagged_export import export_single_flagged_asset
from faceit_ai.settings import CollectSettings, Settings
from faceit_ai.vision.face_crop import parse_bbox_json
from faceit_ai.vision.image_loader import ImageDecodeError, load_image_for_pipeline


@dataclass(frozen=True)
class ReviewFaceInfo:
    face_id: int
    bbox: list[float]
    person_name: str | None
    match_score: float | None


@dataclass(frozen=True)
class ReviewAssetSummary:
    asset_id: int
    path: str
    reason: str
    faces: tuple[ReviewFaceInfo, ...]
    missing_on_disk: bool


@dataclass(frozen=True)
class ReviewAssetDetail(ReviewAssetSummary):
    preview_w: int
    preview_h: int
    usage: str


@dataclass(frozen=True)
class FaceAssignment:
    face_id: int
    person_name: str


@dataclass(frozen=True)
class ConfirmReviewResult:
    asset_id: int
    crops_written: int
    embeddings_added: int
    exported: bool
    metadata_applied: bool


@dataclass(frozen=True)
class BatchConfirmReviewResult:
    moved: int
    skipped: int
    errors: int
    total_crops: int
    total_embeddings: int
    skipped_items: tuple[str, ...]
    error_items: tuple[str, ...]


def _resolved_folder(folder: Path) -> Path:
    return folder.expanduser().resolve()


def path_under_folder(path: Path, folder: Path) -> bool:
    """True if ``path`` lives under ``folder`` (works across Mac/Windows mount spellings)."""
    from faceit_ai.services.processing_runs import asset_path_in_folder

    return asset_path_in_folder(path, folder)


def _load_faces_for_asset(session: Session, asset_id: int) -> list[ReviewFaceInfo]:
    stmt = (
        select(AssetFace, Person.name)
        .outerjoin(Person, Person.id == AssetFace.match_person_id)
        .where(AssetFace.asset_id == asset_id)
        .order_by(AssetFace.id)
    )
    out: list[ReviewFaceInfo] = []
    for face, person_name in session.execute(stmt):
        try:
            bbox = list(parse_bbox_json(str(face.bbox)))
        except (ValueError, json.JSONDecodeError):
            continue
        out.append(
            ReviewFaceInfo(
                face_id=int(face.id),
                bbox=bbox,
                person_name=str(person_name).strip() if person_name else None,
                match_score=float(face.match_score) if face.match_score is not None else None,
            )
        )
    return out


def _face_info_to_dict(f: ReviewFaceInfo) -> dict[str, Any]:
    return {
        "face_id": f.face_id,
        "bbox": f.bbox,
        "person_name": f.person_name,
        "match_score": f.match_score,
    }


def _summary_to_dict(s: ReviewAssetSummary) -> dict[str, Any]:
    names = sorted({f.person_name for f in s.faces if f.person_name})
    return {
        "asset_id": s.asset_id,
        "path": s.path,
        "name": Path(s.path).name,
        "reason": s.reason,
        "face_count": len(s.faces),
        "detected_names": names,
        "faces": [_face_info_to_dict(f) for f in s.faces],
        "missing_on_disk": s.missing_on_disk,
    }


DecisionStatus = Literal["review", "blocked"]


def _normalize_status(status: str) -> DecisionStatus:
    s = (status or "review").strip().lower()
    if s not in ("review", "blocked"):
        return "review"
    return s  # type: ignore[return-value]


def list_review_assets(
    session: Session,
    folder: Path,
    *,
    status: DecisionStatus = "review",
) -> list[ReviewAssetSummary]:
    root = _resolved_folder(folder)
    want = _normalize_status(status)
    stmt = (
        select(Asset, AssetDecision)
        .join(AssetDecision, AssetDecision.asset_id == Asset.id)
        .where(AssetDecision.status == want)
        .order_by(Asset.path)
    )
    out: list[ReviewAssetSummary] = []
    for asset, decision in session.execute(stmt):
        p = Path(str(asset.path)).expanduser()
        try:
            pr = p.resolve()
        except OSError:
            continue
        if not path_under_folder(pr, root):
            continue
        faces = _load_faces_for_asset(session, int(asset.id))
        out.append(
            ReviewAssetSummary(
                asset_id=int(asset.id),
                path=str(pr),
                reason=str(decision.reason),
                faces=tuple(faces),
                missing_on_disk=not pr.is_file(),
            )
        )
    return out


def list_review_assets_json(
    session: Session,
    folder: Path,
    *,
    status: DecisionStatus = "review",
) -> list[dict[str, Any]]:
    return [_summary_to_dict(s) for s in list_review_assets(session, folder, status=status)]


def count_review_assets_by_status(session: Session, folder: Path) -> dict[str, int]:
    """Count review and blocked assets under ``folder`` (for gallery tabs)."""
    root = _resolved_folder(folder)
    counts = {"review": 0, "blocked": 0}
    stmt = (
        select(Asset.path, AssetDecision.status)
        .join(AssetDecision, AssetDecision.asset_id == Asset.id)
        .where(AssetDecision.status.in_(("review", "blocked")))
    )
    for path_str, status in session.execute(stmt):
        if status not in counts:
            continue
        p = Path(str(path_str)).expanduser()
        try:
            pr = p.resolve()
        except OSError:
            continue
        if path_under_folder(pr, root):
            counts[str(status)] += 1
    return counts


def load_review_asset_detail(
    session: Session,
    asset_id: int,
    folder: Path,
    *,
    image_cfg: Any,
    status: DecisionStatus = "review",
) -> ReviewAssetDetail | None:
    root = _resolved_folder(folder)
    want = _normalize_status(status)
    row = session.execute(
        select(Asset, AssetDecision)
        .join(AssetDecision, AssetDecision.asset_id == Asset.id)
        .where(Asset.id == asset_id, AssetDecision.status == want)
    ).first()
    if row is None:
        return None
    asset, decision = row
    p = Path(str(asset.path)).expanduser()
    try:
        pr = p.resolve()
    except OSError:
        return None
    if not path_under_folder(pr, root):
        return None
    preview_w, preview_h = 0, 0
    if pr.is_file():
        try:
            loaded = load_image_for_pipeline(pr, image_cfg)
            h, w = loaded.bgr.shape[:2]
            preview_w, preview_h = int(w), int(h)
        except ImageDecodeError:
            pass
    faces = _load_faces_for_asset(session, int(asset.id))
    return ReviewAssetDetail(
        asset_id=int(asset.id),
        path=str(pr),
        reason=str(decision.reason),
        faces=tuple(faces),
        missing_on_disk=not pr.is_file(),
        preview_w=preview_w,
        preview_h=preview_h,
        usage=str(decision.usage),
    )


def render_review_preview_jpeg(
    session: Session,
    asset_id: int,
    folder: Path,
    *,
    image_cfg: Any,
    status: DecisionStatus = "review",
) -> bytes | None:
    detail = load_review_asset_detail(
        session, asset_id, folder, image_cfg=image_cfg, status=status
    )
    if detail is None or detail.missing_on_disk:
        return None
    try:
        loaded = load_image_for_pipeline(Path(detail.path), image_cfg)
    except ImageDecodeError:
        return None
    ok, buf = cv2.imencode(".jpg", loaded.bgr, [int(cv2.IMWRITE_JPEG_QUALITY), 90])
    if not ok:
        return None
    return buf.tobytes()


def _face_assignments_from_detected(faces: tuple[ReviewFaceInfo, ...]) -> list[FaceAssignment]:
    """Build face assignments from analyzer-detected person names (batch confirm)."""
    out: list[FaceAssignment] = []
    for f in faces:
        if not f.person_name:
            continue
        n = f.person_name.strip()
        if not n or n in (".", "..") or "/" in n or "\\" in n:
            continue
        out.append(FaceAssignment(face_id=f.face_id, person_name=n))
    return out


def _dest_stem_suffix(person_counts: Counter[str], person_name: str) -> str:
    person_counts[person_name] += 1
    n = person_counts[person_name]
    return "" if n == 1 else f"_f{n}"


@dataclass(frozen=True)
class SaveAssignmentsResult:
    updated: int
    crops_written: int
    embeddings_added: int


def _collect_face_into_person_folder(
    *,
    session: Session,
    face_row: AssetFace,
    asset_id: int,
    source_path: Path,
    person_name: str,
    people_root: Path,
    settings: Settings,
    eff_collect: CollectSettings,
    person_counts: Counter[str],
    repo: ConsentRepository,
    embedding_dim: int,
    audit: logging.Logger | None,
    log: logging.Logger,
    force_embedding: bool = False,
) -> tuple[bool, bool, str | None]:
    """Write crop + link person/embedding. Returns (crop_written, embedding_added, dest)."""
    name = person_name.strip()
    bbox = parse_bbox_json(str(face_row.bbox))
    suffix = _dest_stem_suffix(person_counts, name)
    dest_dir = (people_root.expanduser().resolve() / name).resolve()
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{source_path.stem}{suffix}.jpg"

    crop_written = False
    if _write_cropped_portrait(
        source_path=source_path,
        dest=dest,
        bbox=bbox,
        image_cfg=settings.pipeline.image,
        collect=eff_collect,
        log=log,
    ):
        crop_written = True
        if audit is not None:
            log_collect_audit(
                audit,
                src=str(source_path),
                dest=str(dest),
                person=name,
                action="review_confirm_crop",
                extra={"face_id": int(face_row.id)},
            )
        try:
            upsert_collected_photo(
                session,
                collected_path=dest,
                source_path=source_path,
                asset_id=asset_id,
                person_name=name,
                match_score=float(face_row.match_score)
                if face_row.match_score is not None
                else None,
            )
        except Exception:
            log.exception("review_confirm: failed to record source link for %s", dest)
    else:
        log.warning("review_confirm: crop failed for face %s on %s", face_row.id, source_path)

    person = repo.upsert_person_with_consent(
        name=name,
        consent_given=False,
        usage_social=True,
        usage_web=True,
        usage_internal=True,
        usage_print=True,
    )
    repo.update_consent_for_person_name(name=name, consent_given=False)

    already_same = (
        face_row.match_person_id is not None and int(face_row.match_person_id) == int(person.id)
    )
    embedding_added = False
    if force_embedding or not already_same:
        emb = blob_to_embedding(face_row.embedding, embedding_dim)
        repo.add_embedding(person.id, emb)
        embedding_added = True

    face_row.match_person_id = int(person.id)
    if face_row.match_score is None:
        face_row.match_score = 1.0
    return crop_written, embedding_added, (str(dest) if crop_written else None)


@dataclass(frozen=True)
class ConfirmOkResult:
    asset_id: int
    metadata_applied: bool


def confirm_blocked_ok(
    *,
    session: Session,
    asset_id: int,
    folder: Path,
    settings: Settings,
    metadata: MetadataSyncPort | None = None,
    audit: logging.Logger | None = None,
    logger: logging.Logger | None = None,
) -> ConfirmOkResult:
    """Clear a blocked decision to ok (false-positive / allow publish). No crops."""
    log = logger or logging.getLogger("faceit_ai")
    detail = load_review_asset_detail(
        session,
        asset_id,
        folder,
        image_cfg=settings.pipeline.image,
        status="blocked",
    )
    if detail is None:
        raise ValueError("Asset not found or not in blocked status for this folder")
    if detail.missing_on_disk:
        raise ValueError("Source file missing on disk")

    source_path = Path(detail.path)
    decision = session.scalar(select(AssetDecision).where(AssetDecision.asset_id == asset_id))
    if decision is None:
        raise ValueError("Missing asset decision row")
    decision.status = "ok"
    decision.reason = "cleared_from_blocked"
    decision.manual_override = True
    decision.created_at = datetime.now(UTC)
    session.flush()

    metadata_applied = False
    if metadata is not None and settings.metadata.enabled:
        try:
            metadata.apply(
                MetadataWriteRequest(
                    file_path=str(source_path),
                    status="ok",
                    reason="cleared_from_blocked",
                    usage=detail.usage,
                    face_count=len(detail.faces),
                    faces_identified=None,
                    match_confidence_max=None,
                )
            )
            metadata_applied = True
        except Exception:
            log.exception("confirm_blocked_ok: metadata sync failed for %s", source_path)

    if audit is not None:
        audit.info(
            "confirm_blocked_ok",
            extra={
                "audit": {
                    "event": "confirm_blocked_ok",
                    "asset_id": asset_id,
                    "path": str(source_path),
                    "metadata_applied": metadata_applied,
                }
            },
        )

    return ConfirmOkResult(asset_id=asset_id, metadata_applied=metadata_applied)


def confirm_review_ok(
    *,
    session: Session,
    asset_id: int,
    folder: Path,
    settings: Settings,
    metadata: MetadataSyncPort | None = None,
    audit: logging.Logger | None = None,
    logger: logging.Logger | None = None,
) -> ConfirmOkResult:
    """Clear a review decision to ok (unknown/stranger acceptable). No crops."""
    log = logger or logging.getLogger("faceit_ai")
    detail = load_review_asset_detail(
        session,
        asset_id,
        folder,
        image_cfg=settings.pipeline.image,
        status="review",
    )
    if detail is None:
        raise ValueError("Asset not found or not in review status for this folder")
    if detail.missing_on_disk:
        raise ValueError("Source file missing on disk")

    source_path = Path(detail.path)
    decision = session.scalar(select(AssetDecision).where(AssetDecision.asset_id == asset_id))
    if decision is None:
        raise ValueError("Missing asset decision row")
    decision.status = "ok"
    decision.reason = "cleared_from_review"
    decision.manual_override = True
    decision.created_at = datetime.now(UTC)
    session.flush()

    metadata_applied = False
    if metadata is not None and settings.metadata.enabled:
        try:
            metadata.apply(
                MetadataWriteRequest(
                    file_path=str(source_path),
                    status="ok",
                    reason="cleared_from_review",
                    usage=detail.usage,
                    face_count=len(detail.faces),
                    faces_identified=None,
                    match_confidence_max=None,
                )
            )
            metadata_applied = True
        except Exception:
            log.exception("confirm_review_ok: metadata sync failed for %s", source_path)

    if audit is not None:
        audit.info(
            "confirm_review_ok",
            extra={
                "audit": {
                    "event": "confirm_review_ok",
                    "asset_id": asset_id,
                    "path": str(source_path),
                    "metadata_applied": metadata_applied,
                }
            },
        )

    return ConfirmOkResult(asset_id=asset_id, metadata_applied=metadata_applied)


def save_review_face_assignments(
    *,
    session: Session,
    asset_id: int,
    folder: Path,
    face_assignments: list[FaceAssignment],
    image_cfg: Any,
    status: DecisionStatus = "review",
    settings: Settings | None = None,
    people_root: Path | None = None,
    audit: logging.Logger | None = None,
    logger: logging.Logger | None = None,
    embedding_dim: int = 512,
) -> SaveAssignmentsResult:
    """Persist face→person matches without changing decision status.

    Empty ``person_name`` clears the match (explicit Unknown). When ``people_root``
    and ``settings`` are set, named assignments are also cropped into that person's
    folder and (if newly assigned) get an embedding — same as Add faces / Move to blocked.
    """
    log = logger or logging.getLogger("faceit_ai")
    if not face_assignments:
        return SaveAssignmentsResult(updated=0, crops_written=0, embeddings_added=0)
    detail = load_review_asset_detail(
        session, asset_id, folder, image_cfg=image_cfg, status=status
    )
    if detail is None:
        raise ValueError("Asset not found or not in this status for the folder")

    face_by_id = {f.face_id: f for f in detail.faces}
    repo = ConsentRepository(session)
    updated = 0
    crops_written = 0
    embeddings_added = 0
    person_counts: Counter[str] = Counter()
    source_path = Path(detail.path)
    do_collect = people_root is not None and settings is not None and not detail.missing_on_disk
    eff_collect: CollectSettings | None = None
    if do_collect and settings is not None and people_root is not None:
        collect = settings.collect
        eff_collect = CollectSettings(
            people_root=people_root,
            crop_portrait=True,
            crop_aspect_w=collect.crop_aspect_w,
            crop_aspect_h=collect.crop_aspect_h,
            crop_padding=collect.crop_padding,
            output_format=collect.output_format,
        )

    for assign in face_assignments:
        if assign.face_id not in face_by_id:
            raise ValueError(f"Face id {assign.face_id} does not belong to this asset")
        face_row = session.get(AssetFace, assign.face_id)
        if face_row is None or int(face_row.asset_id) != asset_id:
            raise ValueError(f"Invalid face id {assign.face_id}")

        name = assign.person_name.strip()
        if not name:
            face_row.match_person_id = None
            face_row.match_score = None
        elif do_collect and settings is not None and people_root is not None and eff_collect is not None:
            crop_ok, emb_ok, _dest = _collect_face_into_person_folder(
                session=session,
                face_row=face_row,
                asset_id=asset_id,
                source_path=source_path,
                person_name=name,
                people_root=people_root,
                settings=settings,
                eff_collect=eff_collect,
                person_counts=person_counts,
                repo=repo,
                embedding_dim=embedding_dim,
                audit=audit,
                log=log,
            )
            if crop_ok:
                crops_written += 1
            if emb_ok:
                embeddings_added += 1
        else:
            person = repo.get_active_person_by_name(name)
            if person is None:
                person = repo.upsert_person_with_consent(
                    name=name,
                    consent_given=False,
                    usage_social=True,
                    usage_web=True,
                    usage_internal=True,
                    usage_print=True,
                )
            face_row.match_person_id = int(person.id)
            if face_row.match_score is None:
                face_row.match_score = 1.0
        updated += 1
    session.flush()
    return SaveAssignmentsResult(
        updated=updated,
        crops_written=crops_written,
        embeddings_added=embeddings_added,
    )


def confirm_review_blocked(
    *,
    session: Session,
    asset_id: int,
    folder: Path,
    face_assignments: list[FaceAssignment],
    settings: Settings,
    people_root: Path,
    metadata: MetadataSyncPort | None = None,
    export_action: Literal["off", "copy", "move"] = "off",
    audit: logging.Logger | None = None,
    logger: logging.Logger | None = None,
    embedding_dim: int = 512,
    status: DecisionStatus = "review",
) -> ConfirmReviewResult:
    log = logger or logging.getLogger("faceit_ai")
    if not face_assignments:
        raise ValueError("At least one face assignment is required")

    want = _normalize_status(status)
    detail = load_review_asset_detail(
        session, asset_id, folder, image_cfg=settings.pipeline.image, status=want
    )
    if detail is None:
        raise ValueError("Asset not found or not in this status for the folder")
    if detail.missing_on_disk:
        raise ValueError("Source file missing on disk")

    source_path = Path(detail.path)
    face_by_id = {f.face_id: f for f in detail.faces}
    collect = settings.collect
    eff_collect = CollectSettings(
        people_root=people_root,
        crop_portrait=True,
        crop_aspect_w=collect.crop_aspect_w,
        crop_aspect_h=collect.crop_aspect_h,
        crop_padding=collect.crop_padding,
        output_format=collect.output_format,
    )
    repo = ConsentRepository(session)
    person_counts: Counter[str] = Counter()
    crops_written = 0
    embeddings_added = 0
    crop_destinations: list[str] = []
    already_blocked = want == "blocked"

    for assign in face_assignments:
        name = assign.person_name.strip()
        if not name:
            raise ValueError("Person name is required for each face assignment")
        if assign.face_id not in face_by_id:
            raise ValueError(f"Face id {assign.face_id} does not belong to this asset")

        face_row = session.get(AssetFace, assign.face_id)
        if face_row is None or int(face_row.asset_id) != asset_id:
            raise ValueError(f"Invalid face id {assign.face_id}")

        crop_ok, emb_ok, dest = _collect_face_into_person_folder(
            session=session,
            face_row=face_row,
            asset_id=asset_id,
            source_path=source_path,
            person_name=name,
            people_root=people_root,
            settings=settings,
            eff_collect=eff_collect,
            person_counts=person_counts,
            repo=repo,
            embedding_dim=embedding_dim,
            audit=audit,
            log=log,
            force_embedding=True,
        )
        if crop_ok:
            crops_written += 1
            if dest:
                crop_destinations.append(dest)
        if emb_ok:
            embeddings_added += 1

    decision = session.scalar(select(AssetDecision).where(AssetDecision.asset_id == asset_id))
    if decision is None:
        raise ValueError("Missing asset decision row")
    if not already_blocked:
        decision.status = "blocked"
        decision.reason = "manual_confirm"
        decision.manual_override = True
        decision.created_at = datetime.now(UTC)
    session.flush()

    exported = False
    if not already_blocked and export_action in ("copy", "move"):
        ok, _missing, _warns = export_single_flagged_asset(
            session=session,
            scan_root=folder,
            source_path=source_path,
            decision_status="blocked",
            action=export_action,
            audit=audit,
            logger=log,
        )
        exported = ok > 0

    metadata_applied = False
    if not already_blocked and metadata is not None and settings.metadata.enabled:
        try:
            metadata.apply(
                MetadataWriteRequest(
                    file_path=str(source_path),
                    status="blocked",
                    reason="manual_confirm",
                    usage=detail.usage,
                    face_count=len(detail.faces),
                    faces_identified=len(face_assignments),
                    match_confidence_max=None,
                )
            )
            metadata_applied = True
        except Exception:
            log.exception("review_confirm: metadata sync failed for %s", source_path)

    if audit is not None:
        audit.info(
            "review_confirm",
            extra={
                "audit": {
                    "event": "review_confirm" if not already_blocked else "blocked_add_faces",
                    "asset_id": asset_id,
                    "path": str(source_path),
                    "assignments": [
                        {"face_id": a.face_id, "person": a.person_name} for a in face_assignments
                    ],
                    "crops": crop_destinations,
                    "embeddings_added": embeddings_added,
                    "exported": exported,
                }
            },
        )

    return ConfirmReviewResult(
        asset_id=asset_id,
        crops_written=crops_written,
        embeddings_added=embeddings_added,
        exported=exported,
        metadata_applied=metadata_applied,
    )


def batch_confirm_review_blocked(
    *,
    session_factory: sessionmaker[Session],
    folder: Path,
    settings: Settings,
    people_root: Path,
    metadata: MetadataSyncPort | None = None,
    export_action: Literal["off", "copy", "move"] = "off",
    audit: logging.Logger | None = None,
    logger: logging.Logger | None = None,
) -> BatchConfirmReviewResult:
    """Move all review assets in ``folder`` to blocked using detected person names."""
    log = logger or logging.getLogger("faceit_ai")
    with session_scope(session_factory) as session:
        summaries = list_review_assets(session, folder, status="review")

    moved = skipped = errors = 0
    total_crops = total_embeddings = 0
    skipped_items: list[str] = []
    error_items: list[str] = []

    for summary in summaries:
        label = Path(summary.path).name
        if summary.missing_on_disk:
            skipped += 1
            skipped_items.append(f"{label}: missing on disk")
            continue
        assignments = _face_assignments_from_detected(summary.faces)
        if not assignments:
            skipped += 1
            skipped_items.append(f"{label}: no detected person")
            continue
        try:
            with session_scope(session_factory) as session:
                result = confirm_review_blocked(
                    session=session,
                    asset_id=summary.asset_id,
                    folder=folder,
                    face_assignments=assignments,
                    settings=settings,
                    people_root=people_root,
                    metadata=metadata,
                    export_action=export_action,
                    audit=audit,
                    logger=log,
                )
            moved += 1
            total_crops += result.crops_written
            total_embeddings += result.embeddings_added
        except ValueError as e:
            errors += 1
            error_items.append(f"{label}: {e}")
        except Exception as e:
            errors += 1
            error_items.append(f"{label}: {e}")
            log.exception("batch_confirm_review_blocked failed for %s", summary.path)

    if audit is not None:
        audit.info(
            "batch_confirm_review_blocked",
            extra={
                "audit": {
                    "event": "batch_confirm_review_blocked",
                    "folder": str(folder),
                    "moved": moved,
                    "skipped": skipped,
                    "errors": errors,
                    "total_crops": total_crops,
                    "total_embeddings": total_embeddings,
                }
            },
        )

    return BatchConfirmReviewResult(
        moved=moved,
        skipped=skipped,
        errors=errors,
        total_crops=total_crops,
        total_embeddings=total_embeddings,
        skipped_items=tuple(skipped_items),
        error_items=tuple(error_items),
    )
