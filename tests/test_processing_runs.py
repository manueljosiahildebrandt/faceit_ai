"""Cross-OS folder claim keys for shared-DB multi-machine runs."""

from __future__ import annotations

from faceit_ai.services.processing_runs import asset_path_in_folder, folder_claim_key


def test_folder_claim_key_strips_macos_volumes_prefix() -> None:
    assert folder_claim_key("/Volumes/Foto/jobs/wedding") == "/jobs/wedding"


def test_folder_claim_key_strips_windows_drive() -> None:
    # Path on POSIX still parses "Z:/jobs/wedding" usefully for our splitter.
    assert folder_claim_key("Z:/jobs/wedding") == "/jobs/wedding"
    assert folder_claim_key("Z:\\jobs\\wedding") == "/jobs/wedding"


def test_folder_claim_key_mac_and_windows_same_share_collide() -> None:
    assert folder_claim_key("/Volumes/Foto/jobs/wedding") == folder_claim_key("Z:/jobs/wedding")


def test_asset_path_in_folder_cross_os() -> None:
    # Mac-stored asset, Windows folder picker — Current Status must still count it.
    assert asset_path_in_folder(
        "/Volumes/Foto/jobs/wedding/IMG_001.jpg",
        "Z:\\jobs\\wedding",
    )
    assert asset_path_in_folder(
        "Z:/jobs/wedding/sub/IMG_001.jpg",
        "/Volumes/Foto/jobs/wedding",
    )
    assert not asset_path_in_folder(
        "/Volumes/Foto/jobs/other/IMG_001.jpg",
        "Z:\\jobs\\wedding",
    )
