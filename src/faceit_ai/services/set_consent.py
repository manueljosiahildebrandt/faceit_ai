"""Update consent flags for an existing person (no new embeddings)."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import sessionmaker

from faceit_ai.persistence.repository import ConsentRepository
from faceit_ai.persistence.session import session_scope


def run_set_consent(
    *,
    person_name: str,
    consent_given: bool,
    session_factory: sessionmaker[Any],
) -> None:
    with session_scope(session_factory) as session:
        repo = ConsentRepository(session)
        repo.update_consent_for_person_name(name=person_name, consent_given=consent_given)
