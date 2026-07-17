from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from faceit_ai.persistence.models import Base, Consent, Person
from faceit_ai.persistence.repository import ConsentRepository
from faceit_ai.services.set_consent import run_set_consent


def test_update_consent_revoke() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sf = sessionmaker(engine, expire_on_commit=False)

    with sf() as s:
        p = Person(name="x", active=True)
        s.add(p)
        s.flush()
        s.add(
            Consent(
                person_id=p.id,
                consent_given=True,
                usage_social=True,
                usage_web=True,
                usage_internal=True,
                usage_print=True,
            )
        )
        s.commit()

    run_set_consent(person_name="x", consent_given=False, session_factory=sf)

    with sf() as s:
        c = s.get(Consent, 1)
        assert c is not None
        assert c.consent_given is False
