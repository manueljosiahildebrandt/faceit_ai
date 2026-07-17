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
