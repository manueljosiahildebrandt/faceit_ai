from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from faceit_ai.persistence.models import Asset, AssetDecision, Base
from faceit_ai.services.flagged_export import export_flagged_under_folder


def test_export_flagged_copy_mirrors_relative_path(tmp_path: Path) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sf = sessionmaker(engine, expire_on_commit=False)
    root = tmp_path / "teneriffa"
    day1 = root / "day1"
    day1.mkdir(parents=True)
    src = day1 / "sub" / "f.arw"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"x")

    with sf() as s:
        a = Asset(path=str(src.resolve()), sha256="deadbeef")
        s.add(a)
        s.flush()
        s.add(
            AssetDecision(
                asset_id=a.id, status="blocked", reason="no_consent", usage="social"
            )
        )
        s.commit()

    log = logging.getLogger("test_flagged")
    with sf() as s:
        n_ok, n_miss, warns = export_flagged_under_folder(
            session=s,
            scan_root=day1,
            statuses=["blocked"],
            action="copy",
            logger=log,
        )
    assert n_ok == 1
    assert n_miss == 0
    assert not warns
    dest = day1 / "flagged" / "blocked" / "sub" / "f.arw"
    assert dest.read_bytes() == b"x"
    assert src.exists()


def test_export_skips_already_in_flagged_tree(tmp_path: Path) -> None:
    """Files already under ``flagged/review`` (tiered) are not copied again."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sf = sessionmaker(engine, expire_on_commit=False)
    root = tmp_path / "r"
    root.mkdir()
    inside = root / "flagged" / "review" / "keep.arw"
    inside.parent.mkdir(parents=True)
    inside.write_bytes(b"z")

    with sf() as s:
        a = Asset(path=str(inside.resolve()), sha256="aaa")
        s.add(a)
        s.flush()
        s.add(
            AssetDecision(asset_id=a.id, status="review", reason="x", usage="social")
        )
        s.commit()

    with sf() as s:
        n_ok, _, _ = export_flagged_under_folder(
            session=s,
            scan_root=root,
            statuses=["review"],
            action="copy",
            logger=logging.getLogger("t"),
        )
    assert n_ok == 0


def test_export_fallback_db_path_flagged_missing_file_at_scan_root(tmp_path: Path) -> None:
    """DB still has ``root/flagged/name`` but the file only exists as ``root/name``."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sf = sessionmaker(engine, expire_on_commit=False)
    root = tmp_path / "album"
    root.mkdir()
    at_root = root / "keep.arw"
    at_root.write_bytes(b"root")

    with sf() as s:
        a = Asset(path=str((root / "flagged" / "keep.arw").resolve()), sha256="x")
        s.add(a)
        s.flush()
        s.add(AssetDecision(asset_id=a.id, status="blocked", reason="n", usage="social"))
        s.commit()

    with sf() as s:
        n_ok, n_miss, _ = export_flagged_under_folder(
            session=s,
            scan_root=root,
            statuses=["blocked"],
            action="copy",
            logger=logging.getLogger("t"),
        )
    assert n_ok == 1
    assert n_miss == 0
    assert (root / "flagged" / "blocked" / "keep.arw").read_bytes() == b"root"


def test_flat_flagged_folder_migrates_to_tiered_subfolder(tmp_path: Path) -> None:
    """Legacy ``flagged/file`` paths are copied into ``flagged/review/file`` (or blocked)."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sf = sessionmaker(engine, expire_on_commit=False)
    root = tmp_path / "r"
    root.mkdir()
    flat = root / "flagged" / "keep.arw"
    flat.parent.mkdir(parents=True)
    flat.write_bytes(b"m")

    with sf() as s:
        a = Asset(path=str(flat.resolve()), sha256="bbb")
        s.add(a)
        s.flush()
        s.add(AssetDecision(asset_id=a.id, status="review", reason="x", usage="social"))
        s.commit()

    with sf() as s:
        n_ok, _, _ = export_flagged_under_folder(
            session=s,
            scan_root=root,
            statuses=["review"],
            action="copy",
            logger=logging.getLogger("t"),
        )
    assert n_ok == 1
    dest = root / "flagged" / "review" / "keep.arw"
    assert dest.read_bytes() == b"m"
    assert flat.exists()


def test_export_review_goes_under_flagged_review(tmp_path: Path) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sf = sessionmaker(engine, expire_on_commit=False)
    root = tmp_path / "shoot"
    sub = root / "a"
    sub.mkdir(parents=True)
    src = sub / "x.arw"
    src.write_bytes(b"y")

    with sf() as s:
        a = Asset(path=str(src.resolve()), sha256="b")
        s.add(a)
        s.flush()
        s.add(AssetDecision(asset_id=a.id, status="review", reason="x", usage="social"))
        s.commit()

    with sf() as s:
        n_ok, n_miss, _ = export_flagged_under_folder(
            session=s,
            scan_root=root,
            statuses=["review"],
            action="copy",
            logger=logging.getLogger("t"),
        )
    assert n_ok == 1
    assert n_miss == 0
    assert (root / "flagged" / "review" / "a" / "x.arw").read_bytes() == b"y"


def test_export_idempotent_skip_second_run(tmp_path: Path) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sf = sessionmaker(engine, expire_on_commit=False)
    root = tmp_path / "r"
    src = root / "f.arw"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"z")

    with sf() as s:
        a = Asset(path=str(src.resolve()), sha256="c")
        s.add(a)
        s.flush()
        s.add(AssetDecision(asset_id=a.id, status="blocked", reason="n", usage="social"))
        s.commit()

    log = logging.getLogger("idem")
    with sf() as s:
        n1, _, _ = export_flagged_under_folder(
            session=s, scan_root=root, statuses=["blocked"], action="copy", logger=log
        )
        n2, _, _ = export_flagged_under_folder(
            session=s, scan_root=root, statuses=["blocked"], action="copy", logger=log
        )
    assert n1 == 1
    assert n2 == 0
    assert (root / "flagged" / "blocked" / "f.arw").read_bytes() == b"z"
