"""Cross-OS folder claim keys for shared-DB multi-machine runs."""

from __future__ import annotations

from faceit_ai.services.processing_runs import folder_claim_key


def test_folder_claim_key_strips_macos_volumes_prefix() -> None:
    assert folder_claim_key("/Volumes/Foto/jobs/wedding") == "/jobs/wedding"


def test_folder_claim_key_strips_windows_drive() -> None:
    # Path on POSIX still parses "Z:/jobs/wedding" usefully for our splitter.
    assert folder_claim_key("Z:/jobs/wedding") == "/jobs/wedding"
    assert folder_claim_key("Z:\\jobs\\wedding") == "/jobs/wedding"


def test_folder_claim_key_mac_and_windows_same_share_collide() -> None:
    assert folder_claim_key("/Volumes/Foto/jobs/wedding") == folder_claim_key("Z:/jobs/wedding")
