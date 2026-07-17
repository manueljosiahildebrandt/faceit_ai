"""Scan people-folder portraits; optionally re-crop multi-face images."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker
from tqdm import tqdm

from faceit_ai.persistence.models import AssetFace, FaceEmbedding, blob_to_embedding
from faceit_ai.persistence.repository import ConsentRepository
from faceit_ai.persistence.session import session_scope
from faceit_ai.services.collected_photos import lookup_sources_by_collected_paths
from faceit_ai.services.processing_runs import folder_claim_key
from faceit_ai.settings import Settings, load_raw_config
from faceit_ai.vision.face_crop import parse_bbox_json
from faceit_ai.vision.image_loader import ImageDecodeError, load_image_for_pipeline, list_scannable_image_paths
from faceit_ai.vision.insightface_backend import FaceDetectionResult, InsightFaceBackend
from faceit_ai.vision.matcher import cosine_similarity
from faceit_ai.vision.single_face_crop import write_single_face_portrait

_MIN_DET_SCORE = 0.5


def resolve_people_root(settings: Settings) -> Path | None:
    """``collect.people_root`` or ``paths.people_dir`` from YAML."""
    if settings.collect.people_root is not None:
        return settings.collect.people_root
    raw = load_raw_config()
    text = str((raw.get("paths") or {}).get("people_dir") or "").strip()
    if not text:
        return None
    return Path(text).expanduser().resolve()


@dataclass(frozen=True)
class PortraitAuditRow:
    path: Path
    person_folder: str
    face_count: int | None
    detail: str


@dataclass(frozen=True)
class PortraitAuditResult:
    root: Path
    scanned: int
    ok: int
    problems: tuple[PortraitAuditRow, ...]


@dataclass(frozen=True)
class PortraitFixRow:
    path: Path
    person_folder: str
    action: str
    detail: str


@dataclass(frozen=True)
class PortraitFixResult:
    root: Path
    scanned: int
    ok_already: int
    fixed: int
    failed: int
    skipped: int
    rows: tuple[PortraitFixRow, ...]


def audit_people_portraits(
    *,
    settings: Settings,
    backend: InsightFaceBackend,
    people_root: Path | None = None,
    show_progress: bool = True,
    logger: logging.Logger | None = None,
) -> PortraitAuditResult:
    """Count confident faces in each image under ``people_root``."""
    log = logger or logging.getLogger("faceit_ai")
    root = (people_root or resolve_people_root(settings))
    if root is None:
        raise ValueError(
            "People folder not configured. Set collect.people_root or paths.people_dir in config."
        )
    root = root.expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"People folder not found: {root}")

    img_cfg = settings.pipeline.image
    problems: list[PortraitAuditRow] = []
    ok = 0
    paths = list_scannable_image_paths(
        root,
        extensions=img_cfg.scan_extensions(),
        ignore_filename_substrings=img_cfg.ignore_filename_substrings,
    )
    log.info("audit_people_portraits — scanning %d file(s) under %s", len(paths), root)
    log.info("Detecting faces (CPU inference; ~2 min for hundreds of portraits)…")

    for path in tqdm(
        paths,
        desc="audit_people_portraits",
        unit="file",
        disable=not show_progress,
    ):
        try:
            rel = path.resolve().relative_to(root)
            person = rel.parts[0] if rel.parts else "?"
        except ValueError:
            person = "?"

        try:
            bgr = load_image_for_pipeline(path, img_cfg).bgr
            n = sum(1 for f in backend.analyze(bgr) if f.det_score >= _MIN_DET_SCORE)
        except ImageDecodeError as err:
            problems.append(
                PortraitAuditRow(
                    path=path,
                    person_folder=person,
                    face_count=None,
                    detail=f"unreadable: {err}",
                )
            )
            continue

        if n == 1:
            ok += 1
        else:
            problems.append(
                PortraitAuditRow(
                    path=path,
                    person_folder=person,
                    face_count=n,
                    detail=f"{n} faces",
                )
            )

    return PortraitAuditResult(
        root=root,
        scanned=len(paths),
        ok=ok,
        problems=tuple(problems),
    )


def _confident_faces(backend: InsightFaceBackend, bgr) -> list[FaceDetectionResult]:
    return [f for f in backend.analyze(bgr) if f.det_score >= _MIN_DET_SCORE]


def _count_confident_faces(backend: InsightFaceBackend, bgr) -> int:
    return len(_confident_faces(backend, bgr))


def _person_embedding_vectors(session, person_name: str, embedding_dim: int) -> list[np.ndarray]:
    repo = ConsentRepository(session)
    person = repo.get_active_person_by_name(person_name)
    if person is None:
        return []
    rows = session.scalars(
        select(FaceEmbedding.embedding).where(FaceEmbedding.person_id == int(person.id))
    ).all()
    return [blob_to_embedding(blob, embedding_dim) for blob in rows]


def _bbox_from_asset_face(session, asset_id: int, person_id: int) -> tuple[float, float, float, float] | None:
    bbox_json = session.scalar(
        select(AssetFace.bbox)
        .where(
            AssetFace.asset_id == int(asset_id),
            AssetFace.match_person_id == int(person_id),
        )
        .order_by(AssetFace.match_score.desc().nullslast(), AssetFace.id.asc())
        .limit(1)
    )
    if not bbox_json:
        return None
    try:
        return parse_bbox_json(str(bbox_json))
    except (ValueError, TypeError):
        return None


def _pick_face_bbox(
    faces: list[FaceDetectionResult],
    *,
    person_vectors: list[np.ndarray],
) -> tuple[float, float, float, float] | None:
    if not faces:
        return None
    if len(faces) == 1:
        return faces[0].bbox_xyxy
    if person_vectors:
        best_face: FaceDetectionResult | None = None
        best_score = -np.inf
        for face in faces:
            for vec in person_vectors:
                score = cosine_similarity(face.embedding, vec)
                if score > best_score:
                    best_score = score
                    best_face = face
        if best_face is not None:
            return best_face.bbox_xyxy
    return max(faces, key=lambda f: f.det_score).bbox_xyxy


def _resolve_recrop_plan(
    *,
    dest: Path,
    person_name: str,
    settings: Settings,
    backend: InsightFaceBackend,
    session: Any,
) -> tuple[Path, tuple[float, float, float, float] | None, str]:
    """Return ``(source_path, bbox, detail)`` for a people-folder portrait."""
    img_cfg = settings.pipeline.image
    collected = lookup_sources_by_collected_paths(session, [dest]).get(folder_claim_key(dest))
    # Prefer original shoot file + stored face box when we have a collect link.
    if collected is not None:
        source = Path(collected.source_path).expanduser()
        try:
            source_res = source.resolve()
        except OSError:
            source_res = source
        if source_res.is_file():
            if collected.asset_id is not None and collected.person_id is not None:
                bbox = _bbox_from_asset_face(session, int(collected.asset_id), int(collected.person_id))
                if bbox is not None:
                    return source_res, bbox, "source+db_bbox"
            try:
                bgr = load_image_for_pipeline(source_res, img_cfg).bgr
            except ImageDecodeError as err:
                return source_res, None, f"source_unreadable: {err}"
            vectors = _person_embedding_vectors(session, person_name, backend.embedding_dim)
            bbox = _pick_face_bbox(_confident_faces(backend, bgr), person_vectors=vectors)
            if bbox is not None:
                return source_res, bbox, "source+detect"
            return source_res, None, "source_no_face"

    # Manual upload or missing link: detect on the people-folder file itself.
    try:
        bgr = load_image_for_pipeline(dest, img_cfg).bgr
    except ImageDecodeError as err:
        return dest, None, f"unreadable: {err}"
    vectors = _person_embedding_vectors(session, person_name, backend.embedding_dim)
    bbox = _pick_face_bbox(_confident_faces(backend, bgr), person_vectors=vectors)
    if bbox is None:
        return dest, None, "no_face"
    return dest, bbox, "local+detect"


def fix_people_portraits(
    *,
    settings: Settings,
    backend: InsightFaceBackend,
    session_factory: sessionmaker[Any],
    people_root: Path | None = None,
    min_faces: int = 2,
    dry_run: bool = False,
    show_progress: bool = True,
    logger: logging.Logger | None = None,
) -> PortraitFixResult:
    """Scan people portraits and re-crop files with ``min_faces`` or more detected faces."""
    log = logger or logging.getLogger("faceit_ai")
    audit = audit_people_portraits(
        settings=settings,
        backend=backend,
        people_root=people_root,
        show_progress=show_progress,
        logger=log,
    )
    to_fix = [
        row
        for row in audit.problems
        if row.face_count is not None and row.face_count >= int(min_faces)
    ]
    img_cfg = settings.pipeline.image
    rows: list[PortraitFixRow] = []
    fixed = failed = skipped = 0

    log.info(
        "fix_people_portraits — %d file(s) to re-crop (min_faces=%d, dry_run=%s)",
        len(to_fix),
        min_faces,
        dry_run,
    )

    with session_scope(session_factory) as session:
        for row in tqdm(
            to_fix,
            desc="fix_people_portraits",
            unit="file",
            disable=not show_progress,
        ):
            dest = row.path
            if dry_run:
                source, bbox, plan = _resolve_recrop_plan(
                    dest=dest,
                    person_name=row.person_folder,
                    settings=settings,
                    backend=backend,
                    session=session,
                )
                if bbox is None:
                    rows.append(
                        PortraitFixRow(
                            path=dest,
                            person_folder=row.person_folder,
                            action="would_fail",
                            detail=plan,
                        )
                    )
                    failed += 1
                else:
                    rows.append(
                        PortraitFixRow(
                            path=dest,
                            person_folder=row.person_folder,
                            action="would_fix",
                            detail=f"{plan} from {source.name}",
                        )
                    )
                    fixed += 1
                continue

            source, bbox, plan = _resolve_recrop_plan(
                dest=dest,
                person_name=row.person_folder,
                settings=settings,
                backend=backend,
                session=session,
            )
            if bbox is None:
                rows.append(
                    PortraitFixRow(
                        path=dest,
                        person_folder=row.person_folder,
                        action="failed",
                        detail=plan,
                    )
                )
                failed += 1
                continue

            ok = write_single_face_portrait(
                source_path=source,
                dest=dest,
                bbox=bbox,
                image_cfg=img_cfg,
                collect=settings.collect,
                backend=backend,
                log=log,
            )
            if not ok:
                rows.append(
                    PortraitFixRow(
                        path=dest,
                        person_folder=row.person_folder,
                        action="failed",
                        detail="single_face_crop gave up",
                    )
                )
                failed += 1
                continue

            try:
                out_bgr = load_image_for_pipeline(dest, img_cfg).bgr
                n_after = _count_confident_faces(backend, out_bgr)
            except ImageDecodeError as err:
                rows.append(
                    PortraitFixRow(
                        path=dest,
                        person_folder=row.person_folder,
                        action="failed",
                        detail=f"verify unreadable: {err}",
                    )
                )
                failed += 1
                continue

            if n_after == 1:
                rows.append(
                    PortraitFixRow(
                        path=dest,
                        person_folder=row.person_folder,
                        action="fixed",
                        detail=f"{plan} ({row.face_count}→1)",
                    )
                )
                fixed += 1
            else:
                rows.append(
                    PortraitFixRow(
                        path=dest,
                        person_folder=row.person_folder,
                        action="failed",
                        detail=f"still {n_after} faces after crop",
                    )
                )
                failed += 1

    skipped = len(audit.problems) - len(to_fix)
    return PortraitFixResult(
        root=audit.root,
        scanned=audit.scanned,
        ok_already=audit.ok,
        fixed=fixed,
        failed=failed,
        skipped=skipped,
        rows=tuple(rows),
    )
