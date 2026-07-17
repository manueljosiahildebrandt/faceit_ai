"""Persist links from people-folder collected files back to original shoot paths."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from faceit_ai.persistence.models import Asset, AssetFace, CollectedPhoto, Person
from faceit_ai.services.processing_runs import folder_claim_key


def resolve_asset_id_for_source(session: Session, source_path: Path | str) -> int | None:
    """Find ``Asset.id`` for ``source_path`` (exact path, then claim-key match)."""
    text = str(source_path)
    try:
        resolved = str(Path(source_path).expanduser().resolve())
    except OSError:
        resolved = text

    for candidate in dict.fromkeys((resolved, text)):
        asset_id = session.scalar(select(Asset.id).where(Asset.path == candidate))
        if asset_id is not None:
            return int(asset_id)

    want = folder_claim_key(resolved)
    # Bounded scan: only compare paths that share the same basename.
    name = Path(resolved).name
    if not name:
        return None
    rows = session.execute(select(Asset.id, Asset.path).where(Asset.path.endswith(name))).all()
    for asset_id, path in rows:
        if folder_claim_key(path) == want:
            return int(asset_id)
    return None


def resolve_person_id(session: Session, person_name: str) -> int | None:
    name = (person_name or "").strip()
    if not name:
        return None
    pid = session.scalar(select(Person.id).where(Person.name == name, Person.active.is_(True)))
    return int(pid) if pid is not None else None


def upsert_collected_photo(
    session: Session,
    *,
    collected_path: Path | str,
    source_path: Path | str,
    asset_id: int | None = None,
    person_id: int | None = None,
    person_name: str | None = None,
    match_score: float | None = None,
) -> CollectedPhoto:
    """Insert or update the link for a people-folder destination file."""
    try:
        dest_resolved = str(Path(collected_path).expanduser().resolve())
    except OSError:
        dest_resolved = str(collected_path)
    key = folder_claim_key(dest_resolved)
    try:
        src_resolved = str(Path(source_path).expanduser().resolve())
    except OSError:
        src_resolved = str(source_path)

    if asset_id is None:
        asset_id = resolve_asset_id_for_source(session, src_resolved)
    if person_id is None and person_name:
        person_id = resolve_person_id(session, person_name)

    row = session.scalar(select(CollectedPhoto).where(CollectedPhoto.collected_key == key))
    if row is None:
        row = CollectedPhoto(
            collected_key=key,
            collected_path=dest_resolved,
            source_path=src_resolved,
            asset_id=asset_id,
            person_id=person_id,
            match_score=float(match_score) if match_score is not None else None,
        )
        session.add(row)
    else:
        row.collected_path = dest_resolved
        row.source_path = src_resolved
        row.asset_id = asset_id
        if person_id is not None:
            row.person_id = person_id
        if match_score is not None:
            row.match_score = float(match_score)
    session.flush()
    return row


def delete_collected_photo(session: Session, collected_path: Path | str) -> bool:
    """Remove the DB link for a people-folder file. Returns True if a row was deleted."""
    key = folder_claim_key(collected_path)
    row = session.scalar(select(CollectedPhoto).where(CollectedPhoto.collected_key == key))
    if row is None:
        return False
    session.delete(row)
    session.flush()
    return True


def lookup_sources_by_collected_paths(
    session: Session,
    paths: list[Path | str],
) -> dict[str, CollectedPhoto]:
    """Map ``folder_claim_key(path)`` → row for the given people-folder paths."""
    if not paths:
        return {}
    keys = list(dict.fromkeys(folder_claim_key(p) for p in paths))
    rows = session.scalars(
        select(CollectedPhoto).where(CollectedPhoto.collected_key.in_(keys))
    ).all()
    return {row.collected_key: row for row in rows}


def resolve_match_score_for_collected(
    session: Session,
    row: CollectedPhoto | None,
) -> float | None:
    """Prefer stored ``CollectedPhoto.match_score``; else max AssetFace score for the link."""
    if row is None:
        return None
    if row.match_score is not None:
        return float(row.match_score)
    if row.asset_id is None or row.person_id is None:
        return None
    score = session.scalar(
        select(func.max(AssetFace.match_score)).where(
            AssetFace.asset_id == int(row.asset_id),
            AssetFace.match_person_id == int(row.person_id),
            AssetFace.match_score.is_not(None),
        )
    )
    return float(score) if score is not None else None


# Re-export for callers that already import claim helpers via this module.
__all__ = [
    "delete_collected_photo",
    "lookup_sources_by_collected_paths",
    "resolve_asset_id_for_source",
    "resolve_match_score_for_collected",
    "resolve_person_id",
    "upsert_collected_photo",
]
