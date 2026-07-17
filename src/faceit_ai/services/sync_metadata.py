"""Re-apply metadata from stored DB decisions without re-running face analysis."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from sqlalchemy.orm import sessionmaker
from tqdm import tqdm

from faceit_ai.integration.metadata_port import MetadataSyncPort, MetadataWriteRequest
from faceit_ai.persistence.repository import AssetRepository
from faceit_ai.persistence.session import session_scope
from faceit_ai.settings import Settings
from faceit_ai.vision.image_loader import list_scannable_image_paths


def run_sync_metadata(
    *,
    folder: Path,
    settings: Settings,
    session_factory: sessionmaker[Any],
    metadata: MetadataSyncPort,
    audit: logging.Logger | None = None,
    show_progress: bool = True,
    statuses: tuple[str, ...] = ("blocked", "review"),
) -> dict[str, int]:
    """
    For each image under ``folder`` that exists in the DB with a decision, invoke metadata sync
    using the stored status/reason/usage (no decode, no InsightFace).
    """
    log = logging.getLogger("faceit_ai")
    img = settings.pipeline.image
    paths = list_scannable_image_paths(
        folder,
        extensions=img.scan_extensions(),
        ignore_filename_substrings=img.ignore_filename_substrings,
    )
    n_synced = 0
    n_skipped_no_row = 0
    n_skipped_status = 0
    n_errors = 0
    status_set = {s.strip().lower() for s in statuses if s.strip()}

    path_iter = tqdm(paths, desc="sync_metadata", unit="file", disable=not show_progress)
    for path in path_iter:
        pstr = str(path)
        with session_scope(session_factory) as session:
            repo = AssetRepository(session)
            asset = repo.find_by_path(pstr)
            if asset is None or asset.decision is None:
                n_skipped_no_row += 1
                continue
            d = asset.decision
            if status_set and d.status.lower() not in status_set:
                n_skipped_status += 1
                continue
            n_faces = len(asset.faces)
            identified = 0  # not persisted per-face display; optional field
            req = MetadataWriteRequest(
                file_path=pstr,
                status=d.status,
                reason=d.reason,
                usage=d.usage,
                face_count=n_faces,
                faces_identified=identified,
                match_confidence_max=None,
            )
        try:
            metadata.apply(req)
            n_synced += 1
        except Exception:
            n_errors += 1
            log.exception("sync_metadata failed for %s", pstr)

    (audit or log).info(
        "sync_metadata done: synced=%d, no_db_match=%d, skipped_status=%d, errors=%d, scanned=%d",
        n_synced,
        n_skipped_no_row,
        n_skipped_status,
        n_errors,
        len(paths),
    )

    return {
        "synced": n_synced,
        "skipped_no_db_match": n_skipped_no_row,
        "skipped_status": n_skipped_status,
        "errors": n_errors,
        "scanned": len(paths),
    }
