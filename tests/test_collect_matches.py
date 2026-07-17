from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from faceit_ai.persistence.models import Asset, AssetFace, Base, Person
from faceit_ai.services.collect_matches import (
    collect_strong_matches_under_folder,
    list_strong_match_collect_jobs,
)
from faceit_ai.settings import CollectSettings


def _seed_person(session, name: str) -> int:
    p = Person(name=name, active=True)
    session.add(p)
    session.flush()
    return int(p.id)


def test_list_strong_match_collect_jobs_filters_threshold(tmp_path: Path) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sf = sessionmaker(engine, expire_on_commit=False)
    root = tmp_path / "shoot"
    root.mkdir()
    strong_file = root / "strong.arw"
    review_file = root / "review.arw"
    strong_file.write_bytes(b"s")
    review_file.write_bytes(b"r")

    with sf() as s:
        pid = _seed_person(s, "Max")
        a_strong = Asset(path=str(strong_file.resolve()), sha256="s1", processed_at=None)
        a_review = Asset(path=str(review_file.resolve()), sha256="r1", processed_at=None)
        s.add_all([a_strong, a_review])
        s.flush()
        s.add(
            AssetFace(
                asset_id=a_strong.id,
                bbox="[]",
                embedding=b"\x00" * 8,
                match_person_id=pid,
                match_score=300.0,
            )
        )
        s.add(
            AssetFace(
                asset_id=a_review.id,
                bbox="[]",
                embedding=b"\x00" * 8,
                match_person_id=pid,
                match_score=250.0,
            )
        )
        s.commit()

    with sf() as s:
        jobs = list_strong_match_collect_jobs(s, root, match_threshold=295.0)

    assert len(jobs) == 1
    assert jobs[0][0] == strong_file.resolve()
    assert "Max" in jobs[0][1]


def test_collect_strong_matches_copies_to_person_folder(tmp_path: Path) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sf = sessionmaker(engine, expire_on_commit=False)
    root = tmp_path / "shoot"
    people = tmp_path / "people"
    root.mkdir()
    src = root / "kid.arw"
    src.write_bytes(b"photo")

    with sf() as s:
        pid = _seed_person(s, "Anna")
        a = Asset(path=str(src.resolve()), sha256="k1", processed_at=None)
        s.add(a)
        s.flush()
        s.add(
            AssetFace(
                asset_id=a.id,
                bbox="[]",
                embedding=b"\x00" * 8,
                match_person_id=pid,
                match_score=310.0,
            )
        )
        s.commit()

    with sf() as s:
        n_assets, n_copies, n_miss, warns = collect_strong_matches_under_folder(
            session=s,
            scan_root=root,
            people_root=people,
            match_threshold_strong=295.0,
            logger=logging.getLogger("test_collect"),
        )

    assert n_assets == 1
    assert n_copies == 1
    assert n_miss == 0
    assert not warns
    dest = people / "Anna" / "kid.arw"
    assert dest.read_bytes() == b"photo"
    assert src.exists()


def test_list_strong_match_collect_jobs_picks_best_bbox(tmp_path: Path) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sf = sessionmaker(engine, expire_on_commit=False)
    root = tmp_path / "shoot"
    root.mkdir()
    f = root / "a.jpg"
    f.write_bytes(b"x")

    with sf() as s:
        pid = _seed_person(s, "Sam")
        a = Asset(path=str(f.resolve()), sha256="a1", processed_at=None)
        s.add(a)
        s.flush()
        s.add(
            AssetFace(
                asset_id=a.id,
                bbox="[0,0,10,10]",
                embedding=b"\x00" * 8,
                match_person_id=pid,
                match_score=300.0,
            )
        )
        s.add(
            AssetFace(
                asset_id=a.id,
                bbox="[50,50,60,60]",
                embedding=b"\x00" * 8,
                match_person_id=pid,
                match_score=310.0,
            )
        )
        s.commit()

    with sf() as s:
        jobs = list_strong_match_collect_jobs(s, root, match_threshold=295.0)

    assert jobs[0][1]["Sam"][0] == (50.0, 50.0, 60.0, 60.0)
    assert jobs[0][1]["Sam"][1] == 310.0


def test_collect_with_crop_writes_jpg(tmp_path: Path) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sf = sessionmaker(engine, expire_on_commit=False)
    root = tmp_path / "shoot"
    people = tmp_path / "people"
    root.mkdir()
    src = root / "face.png"
    # Minimal valid PNG for cv2
    img = np.zeros((120, 100, 3), dtype=np.uint8)
    img[30:90, 25:75] = (200, 180, 160)
    cv2.imwrite(str(src), img)

    with sf() as s:
        pid = _seed_person(s, "Kim")
        a = Asset(path=str(src.resolve()), sha256="k1", processed_at=None)
        s.add(a)
        s.flush()
        s.add(
            AssetFace(
                asset_id=a.id,
                bbox="[25,30,75,90]",
                embedding=b"\x00" * 8,
                match_person_id=pid,
                match_score=320.0,
            )
        )
        s.commit()

    from faceit_ai.settings import ImagePipelineSettings

    image_cfg = ImagePipelineSettings(
        max_dimension=1800,
        supported_extensions=(".jpg", ".jpeg", ".png", ".webp"),
        raw_extensions=(),
        raw_decode_size="half",
        ignore_filename_substrings=(),
    )
    collect = CollectSettings(
        people_root=people,
        crop_portrait=True,
        crop_aspect_w=3.0,
        crop_aspect_h=4.0,
        crop_padding=1.5,
    )

    with sf() as s:
        n_assets, n_copies, n_miss, warns = collect_strong_matches_under_folder(
            session=s,
            scan_root=root,
            people_root=people,
            match_threshold_strong=295.0,
            collect=collect,
            image_cfg=image_cfg,
            logger=logging.getLogger("test_collect"),
        )

    assert n_assets == 1
    assert n_copies == 1
    dest = people / "Kim" / "face.jpg"
    assert dest.is_file()
    assert dest.stat().st_size > 0


def test_collect_crop_failure_falls_back_to_copy(tmp_path: Path) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sf = sessionmaker(engine, expire_on_commit=False)
    root = tmp_path / "shoot"
    people = tmp_path / "people"
    root.mkdir()
    src = root / "broken.arw"
    src.write_bytes(b"not-really-raw")

    with sf() as s:
        pid = _seed_person(s, "Lee")
        a = Asset(path=str(src.resolve()), sha256="l1", processed_at=None)
        s.add(a)
        s.flush()
        s.add(
            AssetFace(
                asset_id=a.id,
                bbox="[10,10,50,50]",
                embedding=b"\x00" * 8,
                match_person_id=pid,
                match_score=300.0,
            )
        )
        s.commit()

    from faceit_ai.settings import ImagePipelineSettings

    image_cfg = ImagePipelineSettings(
        max_dimension=1800,
        supported_extensions=(".jpg",),
        raw_extensions=(".arw",),
        raw_decode_size="half",
        ignore_filename_substrings=(),
    )
    collect = CollectSettings(people_root=people, crop_portrait=True)

    with patch(
        "faceit_ai.services.collect_matches._write_cropped_portrait",
        return_value=False,
    ):
        with sf() as s:
            n_assets, n_copies, _, _ = collect_strong_matches_under_folder(
                session=s,
                scan_root=root,
                people_root=people,
                match_threshold_strong=295.0,
                collect=collect,
                image_cfg=image_cfg,
                logger=logging.getLogger("test_collect"),
            )

    assert n_assets == 1
    assert n_copies == 1
    assert (people / "Lee" / "broken.arw").read_bytes() == b"not-really-raw"
