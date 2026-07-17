"""Data access: keeps services free of SQL details."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from faceit_ai.persistence.models import (
    Asset,
    AssetDecision,
    AssetFace,
    Consent,
    FaceEmbedding,
    Person,
    blob_to_embedding,
    embedding_to_blob,
)


@dataclass(frozen=True)
class StoredEmbedding:
    id: int
    person_id: int
    person_name: str
    vector: Any


class ConsentRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    def list_all_embeddings(self, embedding_dim: int) -> list[StoredEmbedding]:
        stmt = (
            select(FaceEmbedding, Person.name)
            .join(Person, FaceEmbedding.person_id == Person.id)
            .where(Person.active.is_(True))
        )
        rows = self._s.execute(stmt).all()
        out: list[StoredEmbedding] = []
        for fe, name in rows:
            out.append(
                StoredEmbedding(
                    id=fe.id,
                    person_id=fe.person_id,
                    person_name=name,
                    vector=blob_to_embedding(fe.embedding, embedding_dim),
                )
            )
        return out

    def get_consent(self, person_id: int) -> Consent | None:
        return self._s.get(Consent, person_id)

    def get_active_person_by_name(self, name: str) -> Person | None:
        return self._s.scalar(select(Person).where(Person.name == name, Person.active.is_(True)))

    def upsert_person_with_consent(
        self,
        *,
        name: str,
        consent_given: bool,
        usage_social: bool,
        usage_web: bool,
        usage_internal: bool,
        usage_print: bool,
    ) -> Person:
        existing = self.get_active_person_by_name(name)
        if existing is not None:
            # Adding more reference photos must not silently rewrite GDPR consent flags.
            if self._s.get(Consent, existing.id) is None:
                self._s.add(
                    Consent(
                        person_id=existing.id,
                        consent_given=consent_given,
                        usage_social=usage_social,
                        usage_web=usage_web,
                        usage_internal=usage_internal,
                        usage_print=usage_print,
                    )
                )
            return existing

        # Soft-deactivated person with same name: reactivate and keep embeddings.
        inactive = self._s.scalar(
            select(Person)
            .where(Person.name == name, Person.active.is_(False))
            .order_by(Person.id.desc())
        )
        if inactive is not None:
            inactive.active = True
            if self._s.get(Consent, inactive.id) is None:
                self._s.add(
                    Consent(
                        person_id=inactive.id,
                        consent_given=consent_given,
                        usage_social=usage_social,
                        usage_web=usage_web,
                        usage_internal=usage_internal,
                        usage_print=usage_print,
                    )
                )
            return inactive

        p = Person(name=name, active=True)
        self._s.add(p)
        self._s.flush()
        self._s.add(
            Consent(
                person_id=p.id,
                consent_given=consent_given,
                usage_social=usage_social,
                usage_web=usage_web,
                usage_internal=usage_internal,
                usage_print=usage_print,
            )
        )
        return p

    def update_consent_for_person_name(
        self,
        *,
        name: str,
        consent_given: bool,
        usage_social: bool | None = None,
        usage_web: bool | None = None,
        usage_internal: bool | None = None,
        usage_print: bool | None = None,
    ) -> Person:
        """Update or insert consent for an active person (for testing / admin without re-registering photos)."""
        p = self.get_active_person_by_name(name)
        if p is None:
            raise ValueError(f"No active person named {name!r}")
        c = self.get_consent(p.id)
        if c is None:
            self._s.add(
                Consent(
                    person_id=p.id,
                    consent_given=consent_given,
                    usage_social=usage_social if usage_social is not None else True,
                    usage_web=usage_web if usage_web is not None else True,
                    usage_internal=usage_internal if usage_internal is not None else True,
                    usage_print=usage_print if usage_print is not None else True,
                )
            )
            return p
        c.consent_given = consent_given
        if usage_social is not None:
            c.usage_social = usage_social
        if usage_web is not None:
            c.usage_web = usage_web
        if usage_internal is not None:
            c.usage_internal = usage_internal
        if usage_print is not None:
            c.usage_print = usage_print
        c.updated_at = datetime.now(UTC)
        return p

    def add_embedding(self, person_id: int, vector: Any) -> FaceEmbedding:
        fe = FaceEmbedding(person_id=person_id, embedding=embedding_to_blob(vector))
        self._s.add(fe)
        self._s.flush()
        return fe


class AssetRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    def find_by_sha256(self, sha256: str) -> Asset | None:
        return self._s.scalar(select(Asset).where(Asset.sha256 == sha256))

    def find_by_path(self, path: str) -> Asset | None:
        return self._s.scalar(select(Asset).where(Asset.path == path))

    def _delete_asset_cascade(self, asset: Asset) -> None:
        for af in list(asset.faces):
            self._s.delete(af)
        if asset.decision is not None:
            self._s.delete(asset.decision)
        self._s.delete(asset)

    def mark_processed(
        self,
        *,
        path: str,
        sha256: str,
        faces: list[tuple[str, Any, int | None, float | None]],
        decision_status: str,
        decision_reason: str,
        usage: str,
    ) -> Asset:
        now = datetime.now(UTC)
        # Prefer the row for this exact path so scans of originals vs exported copies stay consistent.
        # If we matched by SHA first, two rows with the same content (same SHA) but different paths
        # could cause UNIQUE(path) failures when moving the "other" row onto a path already owned.
        asset = self.find_by_path(path) or self.find_by_sha256(sha256)
        if asset is None:
            asset = Asset(path=path, sha256=sha256)
            self._s.add(asset)
            self._s.flush()
        else:
            path_owner = self.find_by_path(path)
            if path_owner is not None and path_owner.id != asset.id:
                self._delete_asset_cascade(path_owner)
                self._s.flush()
            asset.path = path
            asset.sha256 = sha256
            # One row per SHA: drop stale duplicates (e.g. same file under originals/ and flagged/).
            for dup in self._s.scalars(
                select(Asset).where(Asset.sha256 == sha256, Asset.id != asset.id)
            ).all():
                self._delete_asset_cascade(dup)
            self._s.flush()
        asset.processed_at = now
        # Replace faces for this asset
        for af in list(asset.faces):
            self._s.delete(af)
        self._s.flush()
        for bbox_json, emb, pid, score in faces:
            self._s.add(
                AssetFace(
                    asset_id=asset.id,
                    bbox=bbox_json,
                    embedding=embedding_to_blob(emb),
                    match_person_id=pid,
                    match_score=score,
                )
            )
        dec = asset.decision
        if dec is None:
            dec = AssetDecision(asset_id=asset.id, status="", reason="", usage="")
            self._s.add(dec)
            self._s.flush()
        if dec.manual_override:
            dec.usage = usage
            dec.created_at = now
        else:
            dec.status = decision_status
            dec.reason = decision_reason
            dec.usage = usage
            dec.created_at = now
            dec.manual_override = False
        self._s.flush()
        return asset

    def is_fully_processed(self, sha256: str) -> bool:
        asset = self.find_by_sha256(sha256)
        if asset is None or asset.processed_at is None:
            return False
        return asset.decision is not None
