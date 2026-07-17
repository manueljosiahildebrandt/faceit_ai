"""End-to-end folder analysis: hash skip, detection, matching, decision, persist, audit."""

from __future__ import annotations

import json
import logging
import signal
import time
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker
from tqdm import tqdm

from faceit_ai.decision.engine import FaceDecisionInput, decide_image
from faceit_ai.integration.metadata_port import MetadataSyncPort, MetadataWriteRequest
from faceit_ai.logging_setup import (
    PHASE_CHECK,
    PHASE_END,
    PHASE_START,
    format_elapsed,
    log_decision,
    log_run_banner,
    log_run_phase,
    run_banner_lines,
)
from faceit_ai.persistence.models import Consent
from faceit_ai.persistence.repository import AssetRepository, ConsentRepository
from faceit_ai.persistence.session import session_scope
from faceit_ai.reporting import log_decision_database_summary, query_decision_summary
from faceit_ai.settings import CollectSettings, ImagePipelineSettings, IngestOrder, Settings
from faceit_ai.vision.image_loader import (
    ImageDecodeError,
    file_digest_sha256,
    list_scannable_image_paths,
    load_image_for_pipeline,
)
from faceit_ai.services.collect_matches import collect_strong_matches_under_folder
from faceit_ai.services.flagged_export import export_flagged_under_folder, prune_stale_flagged_exports
from faceit_ai.services.folder_ingest import run_folder_ingest
from faceit_ai.services.processing_runs import heartbeat
from faceit_ai.vision.insightface_backend import InsightFaceBackend
from faceit_ai.vision.matcher import match_embedding

_cancel_requested = False
_stopped_early = False


def _request_cancel(*_args: object) -> None:
    global _cancel_requested
    _cancel_requested = True


def _install_stop_handlers() -> None:
    signal.signal(signal.SIGTERM, _request_cancel)
    signal.signal(signal.SIGINT, _request_cancel)


def was_analyze_stopped_early() -> bool:
    """True when the last ``run_analyze`` exited because the user requested stop."""
    return _stopped_early


def _load_consent_map(session: Any) -> dict[int, Consent | None]:
    rows = session.scalars(select(Consent)).all()
    return {c.person_id: c for c in rows}


def run_analyze(
    *,
    folder: Path,
    usage: str,
    settings: Settings,
    session_factory: sessionmaker[Any],
    backend: InsightFaceBackend,
    audit: logging.Logger,
    metadata: MetadataSyncPort | None = None,
    force: bool = False,
    json_out_dir: Path | None = None,
    show_progress: bool = True,
    export_flagged: Literal["off", "copy", "move"] = "off",
    flagged_statuses: tuple[str, ...] = ("blocked", "review"),
    collect_people_root: Path | None = None,
    collect_crop: bool | None = None,
    ingest_dest_root: Path | None = None,
    ingest_order: IngestOrder = "copy_then_analyze",
    run_id: int | None = None,
) -> list[dict[str, Any]]:
    global _cancel_requested, _stopped_early
    _cancel_requested = False
    _stopped_early = False
    _install_stop_handlers()

    if usage not in settings.usage_map:
        raise ValueError(f"Unknown usage {usage!r}; expected one of {list(settings.usage_map)}")
    usage_column = settings.usage_map[usage]

    results: list[dict[str, Any]] = []
    img = settings.pipeline.image
    log = logging.getLogger("faceit_ai")
    scan_root = folder
    if ingest_dest_root is not None and ingest_order == "copy_then_analyze":
        ingest_result = run_folder_ingest(
            folder,
            ingest_dest_root,
            skip_flagged_subtree=True,
            audit=audit,
            logger=log,
        )
        scan_root = ingest_result.dest_folder
        log.info(
            "folder_ingest — copy_then_analyze | analyze on archive | dest=%s | copied=%d skipped_identical=%d",
            scan_root,
            ingest_result.n_copied,
            ingest_result.n_skipped,
        )
    elif ingest_dest_root is not None:
        log.info(
            "folder_ingest — analyze_then_copy | analyze on source first | source=%s",
            folder,
        )
    paths = list_scannable_image_paths(
        scan_root,
        extensions=img.scan_extensions(),
        ignore_filename_substrings=img.ignore_filename_substrings,
        exclude_flagged_subtree=True,
    )
    t0 = time.perf_counter()
    _scan = "this_scan"

    log_run_banner(log, "faceit_ai | analyze_photos | start", phase=PHASE_START)
    log_run_phase(
        log,
        PHASE_START,
        "analyze_photos — starting | folder=%s | scan_root=%s | usage=%s | files_to_scan=%d",
        folder,
        scan_root,
        usage,
        len(paths),
    )
    log.info("%s |   files listed in folder: %d", _scan, len(paths))
    log_run_phase(
        log,
        PHASE_CHECK,
        "Loading embedding gallery from database…",
    )
    # Snapshot gallery once per run (deterministic for the batch); re-open session per image for commits.
    with session_scope(session_factory) as session:
        gallery = ConsentRepository(session).list_all_embeddings(backend.embedding_dim)
    log_run_phase(
        log,
        PHASE_CHECK,
        "Gallery ready (%d embedding vector(s)). Starting per-file check (decode → detect → match → decide)…",
        len(gallery),
    )

    n_skip_cache = 0
    n_decode_failed = 0
    n_analyzed = 0

    path_iter = tqdm(
        paths,
        desc="Photos",
        unit="file",
        disable=not show_progress,
        smoothing=0.05,
    )

    last_heartbeat = 0.0
    for path in path_iter:
        if run_id is not None and (time.monotonic() - last_heartbeat) >= 15.0:
            try:
                heartbeat(session_factory, run_id)
            except Exception:
                log.debug("processing_run heartbeat failed", exc_info=True)
            last_heartbeat = time.monotonic()
        if _cancel_requested:
            _stopped_early = True
            log.warning(
                "Stop requested — finishing after current progress and running checkout…"
            )
            break

        path_iter.set_postfix_str(
            path.name[:36] + ("…" if len(path.name) > 36 else ""), refresh=False
        )

        with session_scope(session_factory) as session:
            assets = AssetRepository(session)
            sha = file_digest_sha256(path)
            if not force and assets.is_fully_processed(sha):
                log.debug("skip already processed %s", path)
                n_skip_cache += 1
                continue

            consent_by_person = _load_consent_map(session)

            try:
                loaded = load_image_for_pipeline(path, settings.pipeline.image)
            except ImageDecodeError as err:
                log.warning("Unreadable (skipped): %s — %s", path.name, err)
                n_decode_failed += 1
                payload_err: dict[str, Any] = {
                    "file": path.name,
                    "path": str(path),
                    "status": "error",
                    "reason": "decode_failed",
                    "detail": str(err),
                    "faces": [],
                }
                results.append(payload_err)
                log_decision(
                    audit,
                    asset_path=str(path),
                    status="error",
                    reason="decode_failed",
                    usage=usage,
                    faces=[],
                    extra={"sha256": sha, "detail": str(err)},
                )
                if json_out_dir is not None:
                    json_out_dir.mkdir(parents=True, exist_ok=True)
                    (json_out_dir / f"{path.stem}.json").write_text(
                        json.dumps(payload_err, indent=2), encoding="utf-8"
                    )
                continue

            faces = backend.analyze(loaded.bgr)
            n_analyzed += 1

            face_inputs: list[FaceDecisionInput] = []
            rows_for_db: list[tuple[str, Any, int | None, float | None]] = []

            for fd in faces:
                mr = match_embedding(fd.embedding, gallery, settings.matching)
                face_inputs.append(FaceDecisionInput(match=mr))
                bbox_json = json.dumps([round(x, 2) for x in fd.bbox_xyxy])
                rows_for_db.append((bbox_json, fd.embedding, mr.person_id, mr.score))

            agg = decide_image(
                face_inputs=face_inputs,
                consent_lookup=consent_by_person,
                usage_column=usage_column,
                decision_cfg=settings.decision,
            )

            assets.mark_processed(
                path=str(path),
                sha256=sha,
                faces=rows_for_db,
                decision_status=agg.status,
                decision_reason=agg.reason,
                usage=usage,
            )

        payload = {
            "file": path.name,
            "path": str(path),
            "status": agg.status,
            "reason": agg.reason,
            "faces": agg.faces_out,
        }
        results.append(payload)

        log_decision(
            audit,
            asset_path=str(path),
            status=agg.status,
            reason=agg.reason,
            usage=usage,
            faces=agg.faces_out,
            extra={"sha256": sha, "face_count": len(faces)},
        )

        if metadata is not None:
            identified = sum(1 for f in agg.faces_out if f.get("person") is not None)
            confs = [f.get("confidence") for f in agg.faces_out if f.get("confidence") is not None]
            conf_max = max(confs) if confs else None
            try:
                metadata.apply(
                    MetadataWriteRequest(
                        file_path=str(path),
                        status=agg.status,
                        reason=agg.reason,
                        usage=usage,
                        face_count=len(faces),
                        faces_identified=identified,
                        match_confidence_max=conf_max,
                    )
                )
            except Exception:
                logging.getLogger("faceit_ai").exception(
                    "metadata sync failed (non-fatal) for %s", path
                )

        if json_out_dir is not None:
            json_out_dir.mkdir(parents=True, exist_ok=True)
            out_path = json_out_dir / f"{path.stem}.json"
            spec_payload = {
                "file": path.name,
                "status": agg.status,
                "reason": agg.reason,
                "faces": agg.faces_out,
            }
            out_path.write_text(json.dumps(spec_payload, indent=2), encoding="utf-8")

    for line in run_banner_lines(
        "THIS SCAN | this folder only (not the whole database)"
    ):
        log.info("%s", line)
    log.info("%s |   newly analyzed: %d", _scan, n_analyzed)
    log.info("%s |   skipped (already in DB): %d", _scan, n_skip_cache)
    log.info("%s |   decode errors: %d", _scan, n_decode_failed)
    log.info("%s |   files listed in folder: %d", _scan, len(paths))
    if _stopped_early:
        log.info("%s |   stopped early: 1", _scan)

    for line in run_banner_lines(
        "DATABASE TOTALS | entire SQLite file (every path ever recorded)"
    ):
        log.info("%s", line)
    log.info("Querying cumulative counts across the full database…")
    with session_scope(session_factory) as session:
        db_summary = query_decision_summary(session, samples_per_status=0)
    log_decision_database_summary(log, db_summary, prefix="db_all_time")

    if export_flagged in ("copy", "move"):
        log_run_phase(
            log,
            PHASE_CHECK,
            "Pruning stale flagged exports…",
        )
        with session_scope(session_factory) as session:
            prune_stale_flagged_exports(
                session=session,
                scan_root=scan_root,
                action=export_flagged,
                audit=audit,
                logger=log,
            )
        log_run_phase(
            log,
            PHASE_CHECK,
            "Running flagged export (%s)…",
            export_flagged,
        )
        with session_scope(session_factory) as session:
            export_flagged_under_folder(
                session=session,
                scan_root=scan_root,
                statuses=flagged_statuses,
                action=export_flagged,
                audit=audit,
                logger=log,
            )

    if collect_people_root is not None:
        log_run_phase(
            log,
            PHASE_CHECK,
            "Collecting matches to people folders (score>=%.0f)…",
            settings.collect.match_threshold_collect,
        )
        with session_scope(session_factory) as session:
            try:
                cs = settings.collect
                eff_collect = CollectSettings(
                    people_root=collect_people_root,
                    crop_portrait=(
                        collect_crop if collect_crop is not None else cs.crop_portrait
                    ),
                    crop_aspect_w=cs.crop_aspect_w,
                    crop_aspect_h=cs.crop_aspect_h,
                    crop_padding=cs.crop_padding,
                    output_format=cs.output_format,
                    match_threshold_collect=cs.match_threshold_collect,
                )
                collect_strong_matches_under_folder(
                    session=session,
                    scan_root=scan_root,
                    people_root=collect_people_root,
                    match_threshold=cs.match_threshold_collect,
                    collect=eff_collect,
                    image_cfg=settings.pipeline.image,
                    audit=audit,
                    logger=log,
                    overwrite=force,
                    backend=backend,
                )
            except Exception:
                log.exception("collect_strong batch failed (non-fatal)")

    if ingest_dest_root is not None and ingest_order == "analyze_then_copy":
        ingest_result = run_folder_ingest(
            folder,
            ingest_dest_root,
            skip_flagged_subtree=False,
            phase_label="Copying to archive after analyze",
            audit=audit,
            logger=log,
        )
        log.info(
            "folder_ingest — analyze_then_copy | archive includes flagged/ | dest=%s | copied=%d skipped_identical=%d",
            ingest_result.dest_folder,
            ingest_result.n_copied,
            ingest_result.n_skipped,
        )

    elapsed = time.perf_counter() - t0
    end_msg = (
        "analyze_photos — stopped early in %s | checkout done | "
        "this run: analyzed=%d, skipped_cache=%d, decode_errors=%d, listed=%d"
        if _stopped_early
        else "analyze_photos — finished in %s | this run: analyzed=%d, skipped_cache=%d, decode_errors=%d, listed=%d"
    )
    log_run_phase(
        log,
        PHASE_END,
        end_msg,
        format_elapsed(elapsed),
        n_analyzed,
        n_skip_cache,
        n_decode_failed,
        len(paths),
    )
    log_run_banner(log, "faceit_ai | analyze_photos | done", phase=PHASE_END)

    return results
