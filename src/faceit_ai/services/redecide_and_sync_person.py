"""Recompute AssetDecision from current consent and re-apply metadata.

This avoids re-running face detection/embedding:
- Decisions depend on consent + usage + match tiers.
- The DB stores match tiers implicitly via stored scaled match_score.

Used by the web UI when toggling a person's consent so Lightroom labels update immediately.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any
from datetime import UTC, datetime

from sqlalchemy import distinct, select
from sqlalchemy.orm import sessionmaker

from faceit_ai.decision.engine import FaceDecisionInput, decide_image
from faceit_ai.integration.metadata_port import MetadataSyncPort, MetadataWriteRequest
from faceit_ai.persistence.models import Asset, AssetDecision, AssetFace, Consent, Person
from faceit_ai.persistence.session import session_scope
from faceit_ai.vision.matcher import MatchResult


@dataclass(frozen=True)
class RecomputeResult:
    person_name: str
    affected_assets: int
    decisions_updated: int
    metadata_applied: int
    metadata_errors: int


def _iter_asset_ids_for_person(session: Any, person_id: int) -> list[int]:
    stmt = select(distinct(AssetFace.asset_id)).where(AssetFace.match_person_id == person_id)
    return [int(x) for x in session.scalars(stmt).all()]


def _build_match_result_for_face(
    *,
    face: AssetFace,
    person_name: str | None,
    t_rev: float,
    t_strong: float,
) -> MatchResult:
    mpid = face.match_person_id
    score = float(face.match_score or 0.0)
    if mpid is None:
        return MatchResult(
            person_id=None,
            person_name=None,
            score=score,
            is_unknown=True,
            needs_review_uncertain=False,
        )
    is_unknown = score < t_rev
    needs_review_uncertain = (not is_unknown) and (score < t_strong)
    return MatchResult(
        person_id=mpid,
        person_name=person_name,
        score=score,
        is_unknown=is_unknown,
        needs_review_uncertain=needs_review_uncertain,
    )


def run_redecide_and_sync_person(
    *,
    person_name: str,
    consent_allowed: bool,
    settings: Any,  # Settings
    session_factory: sessionmaker[Any],
    metadata: MetadataSyncPort,
    audit: logging.Logger | None = None,
) -> RecomputeResult:
    """Update consent, recompute decisions, and re-apply metadata for affected assets."""
    log = logging.getLogger("faceit_ai")
    t0 = time.perf_counter()

    with session_scope(session_factory) as session:
        person = session.scalar(select(Person).where(Person.name == person_name))
        if person is None:
            raise ValueError(f"No person named {person_name!r}")

        # 1) Update consent flag.
        consent = session.scalar(select(Consent).where(Consent.person_id == person.id))
        if consent is None:
            # If consent row is missing, create it with default usage flags enabled.
            session.add(
                Consent(
                    person_id=person.id,
                    consent_given=consent_allowed,
                    usage_social=True,
                    usage_web=True,
                    usage_internal=True,
                    usage_print=True,
                )
            )
            session.flush()
        else:
            consent.consent_given = bool(consent_allowed)
            session.flush()

        # 2) Identify affected assets by stored face matches for that person.
        asset_ids = _iter_asset_ids_for_person(session, person.id)

        consent_lookup = {c.person_id: c for c in session.scalars(select(Consent)).all()}

        t_rev = float(settings.matching.match_threshold_review)
        t_strong = float(settings.matching.match_threshold_strong)

        decisions_updated = 0
        metadata_reqs: list[MetadataWriteRequest] = []

        for asset_id in asset_ids:
            decision = session.scalar(
                select(AssetDecision).where(AssetDecision.asset_id == asset_id)
            )
            if decision is None:
                continue

            usage_key = str(decision.usage)
            if usage_key not in settings.usage_map:
                continue
            usage_column = settings.usage_map[usage_key]

            # Load all stored faces for this asset (match tiers are encoded by match_score).
            face_rows = session.execute(
                select(
                    AssetFace,
                    Person.name.label("person_name"),
                )
                .outerjoin(Person, Person.id == AssetFace.match_person_id)
                .where(AssetFace.asset_id == asset_id)
            ).all()

            face_inputs: list[FaceDecisionInput] = []
            faces_out = 0
            for face, person_row_name in face_rows:
                mr = _build_match_result_for_face(
                    face=face,
                    person_name=(None if person_row_name is None else str(person_row_name)),
                    t_rev=t_rev,
                    t_strong=t_strong,
                )
                face_inputs.append(FaceDecisionInput(match=mr))
                faces_out += 1

            agg = decide_image(
                face_inputs=face_inputs,
                consent_lookup=consent_lookup,
                usage_column=usage_column,
                decision_cfg=settings.decision,
            )

            if decision.manual_override:
                continue

            # Update stored decision.
            decision.status = agg.status
            decision.reason = agg.reason
            decision.manual_override = False
            decision.created_at = datetime.now(UTC)

            decisions_updated += 1

            asset = session.scalar(select(Asset).where(Asset.id == asset_id))
            if asset is None:
                continue

            # 3) Prepare metadata request for all statuses so labels can clear to OK.
            req = MetadataWriteRequest(
                file_path=asset.path,
                status=agg.status,
                reason=agg.reason,
                usage=str(decision.usage),
                face_count=faces_out,
                faces_identified=0,
                match_confidence_max=None,
            )
            metadata_reqs.append(req)

        # commit happens via context manager

    # 4) Apply metadata outside the DB transaction (can be slow).
    metadata_errors = 0
    metadata_applied = 0
    for req in metadata_reqs:
        try:
            metadata.apply(req)
            metadata_applied += 1
        except Exception:
            metadata_errors += 1
            log.exception("metadata re-sync failed for %s", req.file_path)

    if audit is not None:
        audit.info(
            "redecide_and_sync_person done: person=%r affected_assets=%d decisions_updated=%d "
            "metadata_applied=%d metadata_errors=%d elapsed_s=%.2f",
            person_name,
            len(asset_ids),
            decisions_updated,
            metadata_applied,
            metadata_errors,
            time.perf_counter() - t0,
        )

    # For the UI parsing logic, emit the same summary shape as sync_metadata.
    if metadata_reqs:
        log.info(
            "sync_metadata done: synced=%d, no_db_match=0, skipped_status=0, errors=%d, scanned=%d",
            metadata_applied,
            metadata_errors,
            len(metadata_reqs),
        )

    return RecomputeResult(
        person_name=person_name,
        affected_assets=len(asset_ids),
        decisions_updated=decisions_updated,
        metadata_applied=metadata_applied,
        metadata_errors=metadata_errors,
    )

