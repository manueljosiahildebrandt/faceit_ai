"""One-time copy of an existing SQLite database into another backend (e.g. Postgres).

Primary keys are preserved so foreign keys stay valid. Intended to run once when moving
from local single-user SQLite to a shared server database.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, insert, select
from sqlalchemy.orm import Session

from faceit_ai.persistence.models import (
    Asset,
    AssetDecision,
    AssetFace,
    Consent,
    FaceEmbedding,
    Person,
)
from faceit_ai.persistence.session import init_db

# FK-safe insertion order. processing_run is runtime-only and intentionally not migrated.
_MODELS_IN_ORDER = [Person, Consent, FaceEmbedding, Asset, AssetFace, AssetDecision]


def _sqlite_url_for(source: Path) -> str:
    return f"sqlite:///{source.expanduser().resolve()}"


def migrate_sqlite_to_url(source_db: Path, target_url: str) -> dict[str, int]:
    """Copy all core tables from a SQLite file into target_url. Returns per-table row counts."""
    src_engine = create_engine(_sqlite_url_for(source_db), future=True)
    tgt_engine = init_db(target_url)  # ensures schema exists on the target

    counts: dict[str, int] = {}
    with Session(src_engine) as src, Session(tgt_engine) as tgt:
        # Guard against accidentally merging into a populated target.
        existing_people = tgt.scalar(select(Person).limit(1))
        if existing_people is not None:
            raise RuntimeError(
                "Target database already contains people; refusing to migrate into a non-empty DB."
            )

        for model in _MODELS_IN_ORDER:
            rows = src.execute(select(model)).scalars().all()
            n = 0
            for row in rows:
                data: dict[str, Any] = {
                    col.name: getattr(row, col.name) for col in model.__table__.columns
                }
                tgt.execute(insert(model.__table__).values(**data))
                n += 1
            counts[model.__tablename__] = n
        tgt.commit()

    return counts
