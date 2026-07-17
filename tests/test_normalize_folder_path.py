"""Folder path normalization for web UI paste/picker."""

from __future__ import annotations

from faceit_ai.web_gui import _normalize_folder_path


def test_normalize_folder_path_strips_quotes() -> None:
    assert _normalize_folder_path('"F:\\Photos\\Event"') == "F:\\Photos\\Event"
    assert _normalize_folder_path("'F:/Photos/Event'") == "F:/Photos/Event"


def test_normalize_folder_path_strips_whitespace() -> None:
    assert _normalize_folder_path("  F:\\A\\B  ") == "F:\\A\\B"
