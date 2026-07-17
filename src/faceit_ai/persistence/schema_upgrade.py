"""Lightweight schema upgrades for existing databases (create_all does not ALTER)."""

from __future__ import annotations

import logging

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

log = logging.getLogger("faceit_ai")

_PERSON_COLUMNS: tuple[tuple[str, str], ...] = (
    ("first_name", "VARCHAR(128)"),
    ("last_name", "VARCHAR(128)"),
    ("display_name", "VARCHAR(256)"),
    ("tags_json", "TEXT"),
)

_COLLECTED_PHOTO_COLUMNS: tuple[tuple[str, str], ...] = (
    ("match_score", "FLOAT"),
)


def upgrade_person_profile_columns(engine: Engine) -> None:
    """Add person profile columns if missing."""
    insp = inspect(engine)
    if not insp.has_table("person"):
        return
    existing = {c["name"] for c in insp.get_columns("person")}
    with engine.begin() as conn:
        for col_name, col_type in _PERSON_COLUMNS:
            if col_name in existing:
                continue
            conn.execute(text(f"ALTER TABLE person ADD COLUMN {col_name} {col_type}"))
            log.info("schema upgrade: added person.%s", col_name)


def upgrade_collected_photo_columns(engine: Engine) -> None:
    """Add collected_photo columns if missing."""
    insp = inspect(engine)
    if not insp.has_table("collected_photo"):
        return
    existing = {c["name"] for c in insp.get_columns("collected_photo")}
    with engine.begin() as conn:
        for col_name, col_type in _COLLECTED_PHOTO_COLUMNS:
            if col_name in existing:
                continue
            conn.execute(text(f"ALTER TABLE collected_photo ADD COLUMN {col_name} {col_type}"))
            log.info("schema upgrade: added collected_photo.%s", col_name)


def upgrade_schema(engine: Engine) -> None:
    """Run all lightweight ALTER upgrades."""
    upgrade_person_profile_columns(engine)
    upgrade_collected_photo_columns(engine)
