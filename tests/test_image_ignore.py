from __future__ import annotations

from pathlib import Path

from faceit_ai.vision.image_loader import path_matches_ignore_rules


def test_pano_dng_ignored_by_default_substring() -> None:
    ign = ("-pano",)
    assert path_matches_ignore_rules(Path("DSC06534-Pano.dng"), ign)
    assert path_matches_ignore_rules(Path("DSC06544-Pano-2.dng"), ign)
    assert not path_matches_ignore_rules(Path("DSC06533.ARW"), ign)
