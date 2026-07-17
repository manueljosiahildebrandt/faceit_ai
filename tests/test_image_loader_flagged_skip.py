from __future__ import annotations

from pathlib import Path

from faceit_ai.vision.image_loader import (
    list_scannable_image_paths,
    path_is_under_flagged_tree,
)


def test_path_is_under_flagged_tree() -> None:
    root = Path("/shoot")
    assert path_is_under_flagged_tree(Path("/shoot/flagged/blocked/a.jpg"), root)
    assert path_is_under_flagged_tree(Path("/shoot/flagged/a.jpg"), root)
    assert not path_is_under_flagged_tree(Path("/shoot/sub/a.jpg"), root)
    assert not path_is_under_flagged_tree(Path("/other/flagged/a.jpg"), root)


def test_list_scannable_image_paths_excludes_flagged(tmp_path: Path) -> None:
    root = tmp_path / "shoot"
    (root / "ok").mkdir(parents=True)
    (root / "flagged" / "blocked").mkdir(parents=True)
    (root / "ok" / "keep.jpg").write_bytes(b"j")
    (root / "flagged" / "blocked" / "skip.jpg").write_bytes(b"j")

    all_paths = list_scannable_image_paths(
        root,
        extensions=(".jpg",),
        ignore_filename_substrings=(),
        exclude_flagged_subtree=False,
    )
    assert len(all_paths) == 2

    analyze_paths = list_scannable_image_paths(
        root,
        extensions=(".jpg",),
        ignore_filename_substrings=(),
        exclude_flagged_subtree=True,
    )
    assert len(analyze_paths) == 1
    assert analyze_paths[0].name == "keep.jpg"
