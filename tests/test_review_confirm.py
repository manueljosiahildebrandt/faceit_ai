"""Review confirm service tests."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import numpy as np
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from faceit_ai.persistence.models import Asset, AssetDecision, AssetFace, Base, Consent, FaceEmbedding, Person
from faceit_ai.persistence.repository import AssetRepository, ConsentRepository
from faceit_ai.services.review_confirm import (
    FaceAssignment,
    batch_confirm_review_blocked,
    confirm_blocked_ok,
    confirm_review_blocked,
    confirm_review_ok,
    list_review_assets,
    save_review_face_assignments,
)
from faceit_ai.settings import CollectSettings, ImagePipelineSettings


class _FakeSettings:
    def __init__(self, people_root: Path) -> None:
        self.pipeline = type(
            "P",
            (),
            {
                "image": ImagePipelineSettings(
                    max_dimension=800,
                    supported_extensions=(".jpg",),
                    raw_extensions=(),
                    raw_decode_size="half",
                    ignore_filename_substrings=(),
                )
            },
        )()
        self.collect = CollectSettings(
            people_root=people_root,
            crop_portrait=True,
            crop_aspect_w=3.0,
            crop_aspect_h=4.0,
            crop_padding=1.5,
        )
        self.metadata = type("M", (), {"enabled": False})()


def _minimal_settings(people_root: Path) -> _FakeSettings:
    return _FakeSettings(people_root)


def _seed_review_asset(
    session,
    *,
    root: Path,
    filename: str,
    person_name: str,
    bbox: str = "[50,60,150,180]",
) -> tuple[int, int]:
    src = root / filename
    src.write_bytes(b"fake-jpeg")
    person = Person(name=person_name, active=True)
    session.add(person)
    session.flush()
    session.add(
        Consent(
            person_id=person.id,
            consent_given=False,
            usage_social=True,
            usage_web=True,
            usage_internal=True,
            usage_print=True,
        )
    )
    asset = Asset(path=str(src.resolve()), sha256=f"sha-{filename}", processed_at=None)
    session.add(asset)
    session.flush()
    face = AssetFace(
        asset_id=asset.id,
        bbox=bbox,
        embedding=np.zeros(512, dtype=np.float32).tobytes(),
        match_person_id=person.id,
        match_score=220.0,
    )
    session.add(face)
    session.flush()
    session.add(
        AssetDecision(
            asset_id=asset.id,
            status="review",
            reason="possible_no_consent",
            usage="social",
            manual_override=False,
        )
    )
    session.commit()
    return int(asset.id), int(face.id)


def test_list_review_assets_by_folder(tmp_path: Path) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sf = sessionmaker(engine, expire_on_commit=False)
    root = tmp_path / "shoot"
    root.mkdir()
    with sf() as s:
        aid, _ = _seed_review_asset(s, root=root, filename="a.jpg", person_name="Kim")
        items = list_review_assets(s, root)
    assert len(items) == 1
    assert items[0].asset_id == aid
    assert items[0].faces[0].person_name == "Kim"


def test_list_review_assets_filters_by_status(tmp_path: Path) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sf = sessionmaker(engine, expire_on_commit=False)
    root = tmp_path / "shoot"
    root.mkdir()
    with sf() as s:
        review_id, _ = _seed_review_asset(s, root=root, filename="rev.jpg", person_name="Kim")
        blocked_id, _ = _seed_review_asset(s, root=root, filename="blk.jpg", person_name="Lee")
        dec = s.scalar(select(AssetDecision).where(AssetDecision.asset_id == blocked_id))
        assert dec is not None
        dec.status = "blocked"
        dec.reason = "no_consent"
        s.commit()
        review_items = list_review_assets(s, root, status="review")
        blocked_items = list_review_assets(s, root, status="blocked")
    assert [i.asset_id for i in review_items] == [review_id]
    assert [i.asset_id for i in blocked_items] == [blocked_id]


@patch("faceit_ai.services.review_confirm._write_cropped_portrait", return_value=True)
def test_confirm_review_blocked(mock_crop: object, tmp_path: Path) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sf = sessionmaker(engine, expire_on_commit=False)
    root = tmp_path / "shoot"
    people = tmp_path / "people"
    root.mkdir()
    people.mkdir()
    with sf() as s:
        aid, fid = _seed_review_asset(s, root=root, filename="portrait.jpg", person_name="Kim")
        settings = _minimal_settings(people)
        result = confirm_review_blocked(
            session=s,
            asset_id=aid,
            folder=root,
            face_assignments=[FaceAssignment(face_id=fid, person_name="Kim")],
            settings=settings,
            people_root=people,
            export_action="off",
        )
        s.commit()
        dec = s.scalar(select(AssetDecision).where(AssetDecision.asset_id == aid))
        emb_count = s.scalar(select(FaceEmbedding.id).join(Person).where(Person.name == "Kim"))
    assert result.crops_written == 1
    assert result.embeddings_added == 1
    assert dec is not None
    assert dec.status == "blocked"
    assert dec.manual_override is True
    assert emb_count is not None
    assert mock_crop.call_count == 1


@patch("faceit_ai.services.review_confirm._write_cropped_portrait", return_value=True)
def test_add_faces_on_already_blocked(mock_crop: object, tmp_path: Path) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sf = sessionmaker(engine, expire_on_commit=False)
    root = tmp_path / "shoot"
    people = tmp_path / "people"
    root.mkdir()
    people.mkdir()
    with sf() as s:
        aid = _seed_unknown_review_asset(s, root=root, filename="more.jpg")
        dec = s.scalar(select(AssetDecision).where(AssetDecision.asset_id == aid))
        assert dec is not None
        dec.status = "blocked"
        dec.reason = "manual_confirm"
        face = s.scalar(select(AssetFace).where(AssetFace.asset_id == aid))
        assert face is not None
        fid = int(face.id)
        s.commit()
        settings = _minimal_settings(people)
        result = confirm_review_blocked(
            session=s,
            asset_id=aid,
            folder=root,
            face_assignments=[FaceAssignment(face_id=fid, person_name="Kim")],
            settings=settings,
            people_root=people,
            export_action="off",
            status="blocked",
        )
        s.commit()
        face2 = s.get(AssetFace, fid)
        dec2 = s.scalar(select(AssetDecision).where(AssetDecision.asset_id == aid))
        kim = s.scalar(select(Person).where(Person.name == "Kim"))
    assert result.crops_written == 1
    assert result.embeddings_added == 1
    assert result.exported is False
    assert dec2 is not None
    assert dec2.status == "blocked"
    assert kim is not None
    assert face2 is not None
    assert face2.match_person_id == kim.id
    assert mock_crop.call_count == 1


@patch("faceit_ai.services.review_confirm._write_cropped_portrait", return_value=True)
def test_confirm_multi_face_two_people(mock_crop: object, tmp_path: Path) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sf = sessionmaker(engine, expire_on_commit=False)
    root = tmp_path / "shoot"
    people = tmp_path / "people"
    root.mkdir()
    people.mkdir()
    src = root / "group.jpg"
    src.write_bytes(b"jpeg")
    with sf() as s:
        p1 = Person(name="Ann", active=True)
        p2 = Person(name="Bob", active=True)
        s.add_all([p1, p2])
        s.flush()
        asset = Asset(path=str(src.resolve()), sha256="g1", processed_at=None)
        s.add(asset)
        s.flush()
        f1 = AssetFace(
            asset_id=asset.id,
            bbox="[10,10,50,50]",
            embedding=np.zeros(512, dtype=np.float32).tobytes(),
            match_person_id=p1.id,
            match_score=210.0,
        )
        f2 = AssetFace(
            asset_id=asset.id,
            bbox="[100,100,160,160]",
            embedding=np.ones(512, dtype=np.float32).tobytes(),
            match_person_id=p2.id,
            match_score=215.0,
        )
        s.add_all([f1, f2])
        s.flush()
        s.add(
            AssetDecision(
                asset_id=asset.id,
                status="review",
                reason="possible_no_consent",
                usage="social",
            )
        )
        s.commit()
        aid, fid1, fid2 = int(asset.id), int(f1.id), int(f2.id)

    with sf() as s:
        settings = _minimal_settings(people)
        result = confirm_review_blocked(
            session=s,
            asset_id=aid,
            folder=root,
            face_assignments=[
                FaceAssignment(face_id=fid1, person_name="Ann"),
                FaceAssignment(face_id=fid2, person_name="Bob"),
            ],
            settings=settings,
            people_root=people,
            export_action="off",
        )
        s.commit()
    assert result.crops_written == 2
    assert result.embeddings_added == 2
    assert mock_crop.call_count == 2


def test_mark_processed_respects_manual_override(tmp_path: Path) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sf = sessionmaker(engine, expire_on_commit=False)
    with sf() as s:
        asset = Asset(path=str((tmp_path / "x.jpg").resolve()), sha256="x", processed_at=None)
        s.add(asset)
        s.flush()
        s.add(
            AssetDecision(
                asset_id=asset.id,
                status="blocked",
                reason="manual_confirm",
                usage="social",
                manual_override=True,
            )
        )
        s.commit()
        repo = AssetRepository(s)
        repo.mark_processed(
            path=str(asset.path),
            sha256="x",
            faces=[],
            decision_status="review",
            decision_reason="test",
            usage="social",
        )
        s.commit()
        dec = s.scalar(select(AssetDecision).where(AssetDecision.asset_id == asset.id))
    assert dec is not None
    assert dec.status == "blocked"
    assert dec.manual_override is True


def test_confirm_blocked_ok(tmp_path: Path) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sf = sessionmaker(engine, expire_on_commit=False)
    root = tmp_path / "shoot"
    people = tmp_path / "people"
    root.mkdir()
    people.mkdir()
    with sf() as s:
        aid, _ = _seed_review_asset(s, root=root, filename="blocked.jpg", person_name="Kim")
        dec = s.scalar(select(AssetDecision).where(AssetDecision.asset_id == aid))
        assert dec is not None
        dec.status = "blocked"
        dec.reason = "no_consent"
        s.commit()
        settings = _minimal_settings(people)
        result = confirm_blocked_ok(
            session=s,
            asset_id=aid,
            folder=root,
            settings=settings,
        )
        s.commit()
        dec2 = s.scalar(select(AssetDecision).where(AssetDecision.asset_id == aid))
    assert result.asset_id == aid
    assert dec2 is not None
    assert dec2.status == "ok"
    assert dec2.reason == "cleared_from_blocked"
    assert dec2.manual_override is True


def test_confirm_review_ok(tmp_path: Path) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sf = sessionmaker(engine, expire_on_commit=False)
    root = tmp_path / "shoot"
    people = tmp_path / "people"
    root.mkdir()
    people.mkdir()
    with sf() as s:
        aid, _ = _seed_review_asset(s, root=root, filename="review.jpg", person_name="Kim")
        settings = _minimal_settings(people)
        result = confirm_review_ok(
            session=s,
            asset_id=aid,
            folder=root,
            settings=settings,
        )
        s.commit()
        dec2 = s.scalar(select(AssetDecision).where(AssetDecision.asset_id == aid))
    assert result.asset_id == aid
    assert dec2 is not None
    assert dec2.status == "ok"
    assert dec2.reason == "cleared_from_review"
    assert dec2.manual_override is True


def _seed_unknown_review_asset(session, *, root: Path, filename: str) -> int:
    src = root / filename
    src.write_bytes(b"fake-jpeg")
    asset = Asset(path=str(src.resolve()), sha256=f"sha-{filename}", processed_at=None)
    session.add(asset)
    session.flush()
    session.add(
        AssetFace(
            asset_id=asset.id,
            bbox="[50,60,150,180]",
            embedding=np.zeros(512, dtype=np.float32).tobytes(),
            match_person_id=None,
            match_score=None,
        )
    )
    session.flush()
    session.add(
        AssetDecision(
            asset_id=asset.id,
            status="review",
            reason="possible_no_consent",
            usage="social",
            manual_override=False,
        )
    )
    session.commit()
    return int(asset.id)


@patch("faceit_ai.services.review_confirm._write_cropped_portrait", return_value=True)
def test_batch_confirm_review_blocked(mock_crop: object, tmp_path: Path) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sf = sessionmaker(engine, expire_on_commit=False)
    root = tmp_path / "shoot"
    people = tmp_path / "people"
    root.mkdir()
    people.mkdir()
    with sf() as s:
        named_id, _ = _seed_review_asset(s, root=root, filename="named.jpg", person_name="Kim")
        unknown_id = _seed_unknown_review_asset(s, root=root, filename="unknown.jpg")
        settings = _minimal_settings(people)
        result = batch_confirm_review_blocked(
            session_factory=sf,
            folder=root,
            settings=settings,
            people_root=people,
            export_action="off",
        )
        review_left = list_review_assets(s, root, status="review")
        blocked = list_review_assets(s, root, status="blocked")
        dec_named = s.scalar(select(AssetDecision).where(AssetDecision.asset_id == named_id))
        dec_unknown = s.scalar(select(AssetDecision).where(AssetDecision.asset_id == unknown_id))
    assert result.moved == 1
    assert result.skipped == 1
    assert result.errors == 0
    assert "unknown.jpg: no detected person" in result.skipped_items
    assert len(review_left) == 1
    assert review_left[0].asset_id == unknown_id
    assert len(blocked) == 1
    assert blocked[0].asset_id == named_id
    assert dec_named is not None
    assert dec_named.status == "blocked"
    assert dec_unknown is not None
    assert dec_unknown.status == "review"
    assert mock_crop.call_count == 1


def test_save_review_face_assignments_set_and_clear(tmp_path: Path) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sf = sessionmaker(engine, expire_on_commit=False)
    root = tmp_path / "shoot"
    people = tmp_path / "people"
    root.mkdir()
    people.mkdir()
    settings = _minimal_settings(people)
    with sf() as s:
        aid, fid = _seed_review_asset(s, root=root, filename="face.jpg", person_name="Kim")
        face = s.get(AssetFace, fid)
        assert face is not None
        assert face.match_person_id is not None

        cleared = save_review_face_assignments(
            session=s,
            asset_id=aid,
            folder=root,
            face_assignments=[FaceAssignment(face_id=fid, person_name="")],
            image_cfg=settings.pipeline.image,
            status="review",
        )
        s.commit()
        face = s.get(AssetFace, fid)
        assert cleared.updated == 1
        assert face is not None
        assert face.match_person_id is None
        assert face.match_score is None

        assigned = save_review_face_assignments(
            session=s,
            asset_id=aid,
            folder=root,
            face_assignments=[FaceAssignment(face_id=fid, person_name="Kim")],
            image_cfg=settings.pipeline.image,
            status="review",
        )
        s.commit()
        face = s.get(AssetFace, fid)
        kim = s.scalar(select(Person).where(Person.name == "Kim"))
        assert assigned.updated == 1
        assert assigned.crops_written == 0  # no people_root → DB only
        assert face is not None
        assert kim is not None
        assert face.match_person_id == kim.id
        assert face.match_score == 1.0


@patch("faceit_ai.services.review_confirm._write_cropped_portrait", return_value=True)
def test_save_review_face_assignments_crops_into_person_folder(
    mock_crop: object, tmp_path: Path
) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sf = sessionmaker(engine, expire_on_commit=False)
    root = tmp_path / "shoot"
    people = tmp_path / "people"
    root.mkdir()
    people.mkdir()
    settings = _minimal_settings(people)
    with sf() as s:
        aid = _seed_unknown_review_asset(s, root=root, filename="synced.jpg")
        face = s.scalar(select(AssetFace).where(AssetFace.asset_id == aid))
        assert face is not None
        fid = int(face.id)
        dec = s.scalar(select(AssetDecision).where(AssetDecision.asset_id == aid))
        assert dec is not None
        dec.status = "blocked"
        s.commit()

        result = save_review_face_assignments(
            session=s,
            asset_id=aid,
            folder=root,
            face_assignments=[FaceAssignment(face_id=fid, person_name="NewPerson")],
            image_cfg=settings.pipeline.image,
            status="blocked",
            settings=settings,
            people_root=people,
        )
        s.commit()
        face2 = s.get(AssetFace, fid)
        person = s.scalar(select(Person).where(Person.name == "NewPerson"))
        emb = s.scalar(
            select(FaceEmbedding.id).join(Person).where(Person.name == "NewPerson")
        )
    assert result.updated == 1
    assert result.crops_written == 1
    assert result.embeddings_added == 1
    assert person is not None
    assert face2 is not None
    assert face2.match_person_id == person.id
    assert emb is not None
    assert mock_crop.call_count == 1
    dest = mock_crop.call_args.kwargs.get("dest") or mock_crop.call_args[1].get("dest")
    assert dest is not None
    assert dest.parent.name == "NewPerson"
