"""Collected people-folder photo → original source path links."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from faceit_ai.persistence.models import Asset, AssetFace, Base, CollectedPhoto, Person
from faceit_ai.services.collected_photos import (
    delete_collected_photo,
    lookup_sources_by_collected_paths,
    resolve_match_score_for_collected,
    upsert_collected_photo,
)
from faceit_ai.services.processing_runs import folder_claim_key


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)()


def test_upsert_and_lookup_by_claim_key_cross_os() -> None:
    sf = _session()
    with sf as session:
        # Mac-style dest stored; Windows-style path used for lookup (same claim key).
        mac_dest = "/Volumes/Foto/people/anna/DJI_001.jpg"
        win_lookup = "Z:\\people\\anna\\DJI_001.jpg"
        assert folder_claim_key(mac_dest) == folder_claim_key(win_lookup)

        source = "/Volumes/Foto/jobs/wedding/DJI_001.ARW"
        upsert_collected_photo(
            session,
            collected_path=mac_dest,
            source_path=source,
            person_name="anna",
        )
        session.commit()

        found = lookup_sources_by_collected_paths(session, [win_lookup])
        key = folder_claim_key(win_lookup)
        assert key in found
        assert found[key].source_path.endswith("DJI_001.ARW")


def test_lookup_missing_returns_empty() -> None:
    sf = _session()
    with sf as session:
        assert lookup_sources_by_collected_paths(session, ["/tmp/nope.jpg"]) == {}


def test_upsert_updates_source_and_resolves_asset_id(tmp_path: Path) -> None:
    sf = _session()
    with sf as session:
        asset_path = tmp_path / "shoot" / "IMG.jpg"
        asset_path.parent.mkdir(parents=True)
        asset_path.write_bytes(b"x")
        dest = tmp_path / "people" / "bob" / "IMG.jpg"
        dest.parent.mkdir(parents=True)
        dest.write_bytes(b"y")

        session.add(Asset(path=str(asset_path.resolve()), sha256="a" * 64))
        session.add(Person(name="bob", active=True))
        session.commit()

        row = upsert_collected_photo(
            session,
            collected_path=dest,
            source_path=asset_path,
            person_name="bob",
        )
        session.commit()
        assert row.asset_id is not None
        assert row.person_id is not None
        assert Path(row.source_path).name == "IMG.jpg"

        upsert_collected_photo(
            session,
            collected_path=dest,
            source_path=asset_path,
            person_name="bob",
        )
        session.commit()
        count = session.scalar(select(func.count()).select_from(CollectedPhoto))
        assert count == 1


def test_delete_collected_photo(tmp_path: Path) -> None:
    sf = _session()
    with sf as session:
        dest = tmp_path / "people" / "x" / "a.jpg"
        dest.parent.mkdir(parents=True)
        dest.write_bytes(b"z")
        upsert_collected_photo(
            session,
            collected_path=dest,
            source_path="/Volumes/Foto/jobs/a.ARW",
        )
        session.commit()
        assert delete_collected_photo(session, dest) is True
        session.commit()
        assert delete_collected_photo(session, dest) is False


def test_upsert_stores_and_updates_match_score(tmp_path: Path) -> None:
    sf = _session()
    with sf as session:
        dest = tmp_path / "people" / "kim" / "face.jpg"
        dest.parent.mkdir(parents=True)
        dest.write_bytes(b"crop")
        row = upsert_collected_photo(
            session,
            collected_path=dest,
            source_path="/Volumes/Foto/jobs/face.ARW",
            person_name="kim",
            match_score=245.5,
        )
        session.commit()
        assert row.match_score == 245.5

        row2 = upsert_collected_photo(
            session,
            collected_path=dest,
            source_path="/Volumes/Foto/jobs/face.ARW",
            person_name="kim",
            match_score=251.0,
        )
        session.commit()
        assert row2.match_score == 251.0
        assert session.scalar(select(func.count()).select_from(CollectedPhoto)) == 1


def test_resolve_match_score_falls_back_to_asset_face(tmp_path: Path) -> None:
    sf = _session()
    with sf as session:
        asset_path = tmp_path / "shoot" / "IMG.jpg"
        asset_path.parent.mkdir(parents=True)
        asset_path.write_bytes(b"x")
        dest = tmp_path / "people" / "lee" / "IMG.jpg"
        dest.parent.mkdir(parents=True)
        dest.write_bytes(b"y")

        person = Person(name="lee", active=True)
        session.add(person)
        session.flush()
        asset = Asset(path=str(asset_path.resolve()), sha256="b" * 64)
        session.add(asset)
        session.flush()
        session.add(
            AssetFace(
                asset_id=asset.id,
                bbox="[1,2,3,4]",
                embedding=b"\x00" * 8,
                match_person_id=person.id,
                match_score=238.25,
            )
        )
        session.add(
            AssetFace(
                asset_id=asset.id,
                bbox="[5,6,7,8]",
                embedding=b"\x01" * 8,
                match_person_id=person.id,
                match_score=210.0,
            )
        )
        session.commit()

        row = upsert_collected_photo(
            session,
            collected_path=dest,
            source_path=asset_path,
            person_name="lee",
        )
        session.commit()
        assert row.match_score is None
        assert resolve_match_score_for_collected(session, row) == 238.25

        row.match_score = 260.0
        session.commit()
        assert resolve_match_score_for_collected(session, row) == 260.0
