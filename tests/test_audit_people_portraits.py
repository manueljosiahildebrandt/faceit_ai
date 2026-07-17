"""People-folder portrait audit and fix."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from faceit_ai.persistence.models import Asset, AssetFace, Base, Person
from faceit_ai.services.audit_people_portraits import (
    _pick_face_bbox,
    _resolve_recrop_plan,
    fix_people_portraits,
)
from faceit_ai.services.collected_photos import upsert_collected_photo
from faceit_ai.vision.insightface_backend import FaceDetectionResult


def _session_factory():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


def test_pick_face_bbox_uses_person_embedding() -> None:
    vec = np.array([1.0, 0.0], dtype=np.float32)
    other = np.array([0.0, 1.0], dtype=np.float32)
    faces = [
        FaceDetectionResult(bbox_xyxy=(0, 0, 1, 1), det_score=0.99, embedding=other),
        FaceDetectionResult(bbox_xyxy=(5, 5, 6, 6), det_score=0.5, embedding=vec),
    ]
    bbox = _pick_face_bbox(faces, person_vectors=[vec])
    assert bbox == (5, 5, 6, 6)


def test_resolve_recrop_plan_prefers_source_and_db_bbox(tmp_path: Path) -> None:
    people = tmp_path / "people" / "anna"
    people.mkdir(parents=True)
    dest = people / "crop.jpg"
    dest.write_bytes(b"\xff\xd8\xff\xd9")
    source = tmp_path / "shoot" / "raw.jpg"
    source.parent.mkdir()
    source.write_bytes(b"\xff\xd8\xff\xd9")

    sf = _session_factory()
    with sf() as session:
        person = Person(name="anna", active=True)
        session.add(person)
        session.flush()
        asset = Asset(path=str(source.resolve()), sha256="abc123")
        session.add(asset)
        session.flush()
        session.add(
            AssetFace(
                asset_id=asset.id,
                bbox="[10,20,30,40]",
                embedding=b"\x00" * (512 * 4),
                match_person_id=person.id,
                match_score=300.0,
            )
        )
        upsert_collected_photo(
            session,
            collected_path=dest,
            source_path=source,
            asset_id=int(asset.id),
            person_id=int(person.id),
        )
        session.commit()

        settings = MagicMock()
        settings.pipeline.image = MagicMock()
        backend = MagicMock()
        backend.embedding_dim = 512

        source_out, bbox, detail = _resolve_recrop_plan(
            dest=dest,
            person_name="anna",
            settings=settings,
            backend=backend,
            session=session,
        )
        assert source_out.resolve() == source.resolve()
        assert bbox == (10.0, 20.0, 30.0, 40.0)
        assert detail == "source+db_bbox"


def test_fix_people_portraits_dry_run(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "people"
    person_dir = root / "anna"
    person_dir.mkdir(parents=True)
    bad = person_dir / "bad.jpg"
    bad.write_bytes(b"\xff\xd8\xff\xd9")

    audit_row = MagicMock(
        path=bad,
        person_folder="anna",
        face_count=2,
        detail="2 faces",
    )
    audit_result = MagicMock(
        root=root,
        scanned=1,
        ok=0,
        problems=(audit_row,),
    )
    monkeypatch.setattr(
        "faceit_ai.services.audit_people_portraits.audit_people_portraits",
        lambda **_kw: audit_result,
    )
    monkeypatch.setattr(
        "faceit_ai.services.audit_people_portraits._resolve_recrop_plan",
        lambda **_kw: (bad, (1.0, 2.0, 3.0, 4.0), "local+detect"),
    )

    settings = MagicMock()
    settings.collect = MagicMock()
    settings.pipeline.image = MagicMock()
    result = fix_people_portraits(
        settings=settings,
        backend=MagicMock(),
        session_factory=_session_factory(),
        people_root=root,
        dry_run=True,
        show_progress=False,
    )
    assert result.fixed == 1
    assert result.rows[0].action == "would_fix"
