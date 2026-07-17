"""AssetRepository.mark_processed duplicate-path / same-SHA edge cases."""

from __future__ import annotations

import numpy as np
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from faceit_ai.persistence.models import Asset, Base
from faceit_ai.persistence.repository import AssetRepository


def test_mark_processed_same_sha_two_paths_prefers_path_then_drops_stale(tmp_path) -> None:
    """Same bytes under originals/ and flagged/ must not UNIQUE(path) when re-processing."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sf = sessionmaker(engine, expire_on_commit=False)

    p_flagged = str(tmp_path / "flagged" / "x.ARW")
    p_original = str(tmp_path / "x.ARW")
    sha = "a" * 64

    with sf() as s:
        r = AssetRepository(s)
        r.mark_processed(
            path=p_flagged,
            sha256=sha,
            faces=[("[]", np.zeros(512, dtype=np.float32), None, None)],
            decision_status="blocked",
            decision_reason="no_consent",
            usage="social",
        )
        s.commit()

    with sf() as s:
        r = AssetRepository(s)
        r.mark_processed(
            path=p_original,
            sha256=sha,
            faces=[("[]", np.zeros(512, dtype=np.float32), None, None)],
            decision_status="blocked",
            decision_reason="no_consent",
            usage="social",
        )
        s.commit()

    with sf() as s:
        rows = s.query(Asset).where(Asset.sha256 == sha).all()
        assert len(rows) == 1
        assert rows[0].path == p_original
