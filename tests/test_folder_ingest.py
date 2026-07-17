"""Tests for SD card → NAS folder ingest (copy-only archive before analyze)."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from faceit_ai.services.folder_ingest import (
    copy_folder_tree,
    resolve_ingest_destination,
    run_folder_ingest,
)


def test_resolve_ingest_destination_basename(tmp_path: Path) -> None:
    src = tmp_path / "100CANON"
    src.mkdir()
    dest_root = tmp_path / "NAS" / "Ingest"
    dest_root.mkdir(parents=True)
    assert resolve_ingest_destination(src, dest_root) == dest_root / "100CANON"


def test_resolve_rejects_dest_inside_source(tmp_path: Path) -> None:
    src = tmp_path / "shoot"
    src.mkdir()
    dest_root = src / "archive"
    dest_root.mkdir()
    with pytest.raises(ValueError, match="inside the source"):
        resolve_ingest_destination(src, dest_root)


def test_resolve_rejects_source_inside_dest_root(tmp_path: Path) -> None:
    dest_root = tmp_path / "NAS"
    dest_root.mkdir()
    src = dest_root / "Ingest" / "100CANON"
    src.mkdir(parents=True)
    with pytest.raises(ValueError, match="inside the archive destination"):
        resolve_ingest_destination(src, dest_root)


def test_copy_folder_tree_preserves_structure(tmp_path: Path) -> None:
    src = tmp_path / "src" / "100CANON"
    (src / "sub").mkdir(parents=True)
    (src / "a.jpg").write_bytes(b"aaa")
    (src / "sub" / "b.xmp").write_bytes(b"bbb")
    (src / "sidecar.txt").write_bytes(b"meta")

    dest = tmp_path / "dest" / "100CANON"
    n_copied, n_skipped, warnings = copy_folder_tree(src, dest)

    assert n_copied == 3
    assert n_skipped == 0
    assert not warnings
    assert (dest / "a.jpg").read_bytes() == b"aaa"
    assert (dest / "sub" / "b.xmp").read_bytes() == b"bbb"
    assert (src / "a.jpg").exists()


def test_copy_skips_flagged_subtree(tmp_path: Path) -> None:
    src = tmp_path / "shoot"
    flagged = src / "flagged" / "blocked"
    flagged.mkdir(parents=True)
    (src / "ok.jpg").write_bytes(b"1")
    (flagged / "bad.jpg").write_bytes(b"2")

    dest = tmp_path / "archive" / "shoot"
    n_copied, _, _ = copy_folder_tree(src, dest)

    assert n_copied == 1
    assert (dest / "ok.jpg").exists()
    assert not (dest / "flagged").exists()


def test_copy_skips_identical_size(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "f.jpg").write_bytes(b"same")

    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "f.jpg").write_bytes(b"same")

    n_copied, n_skipped, warnings = copy_folder_tree(src, dest)
    assert n_copied == 0
    assert n_skipped == 1
    assert not warnings


def test_copy_warns_on_size_conflict(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "f.jpg").write_bytes(b"new")

    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "f.jpg").write_bytes(b"old-longer-bytes")

    n_copied, n_skipped, warnings = copy_folder_tree(src, dest)
    assert n_copied == 0
    assert n_skipped == 0
    assert any("different size" in w for w in warnings)


def test_copy_includes_flagged_when_not_skipped(tmp_path: Path) -> None:
    src = tmp_path / "shoot"
    flagged = src / "flagged" / "blocked"
    flagged.mkdir(parents=True)
    (src / "ok.jpg").write_bytes(b"1")
    (flagged / "bad.jpg").write_bytes(b"2")

    dest = tmp_path / "archive" / "shoot"
    n_copied, _, _ = copy_folder_tree(src, dest, skip_flagged_subtree=False)

    assert n_copied == 2
    assert (dest / "flagged" / "blocked" / "bad.jpg").exists()


def test_run_folder_ingest_returns_dest_and_counts(tmp_path: Path) -> None:
    src = tmp_path / "DCIM" / "100CANON"
    src.mkdir(parents=True)
    (src / "img.arw").write_bytes(b"raw")
    dest_root = tmp_path / "NAS"
    dest_root.mkdir()

    result = run_folder_ingest(src, dest_root, logger=logging.getLogger("test"))

    assert result.dest_folder == dest_root / "100CANON"
    assert result.n_copied == 1
    assert (result.dest_folder / "img.arw").read_bytes() == b"raw"


def test_run_folder_ingest_idempotent_rerun(tmp_path: Path) -> None:
    src = tmp_path / "card"
    src.mkdir()
    (src / "a.jpg").write_bytes(b"x")
    dest_root = tmp_path / "nas"
    dest_root.mkdir()

    first = run_folder_ingest(src, dest_root, logger=logging.getLogger("test"))
    second = run_folder_ingest(src, dest_root, logger=logging.getLogger("test"))

    assert first.n_copied == 1
    assert second.n_copied == 0
    assert second.n_skipped == 1
