"""People gallery delete from web UI."""

from __future__ import annotations

from pathlib import Path

import pytest

from faceit_ai import web_gui


@pytest.fixture
def people_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    root = tmp_path / "people_root"
    person = root / "Anna"
    person.mkdir(parents=True)
    photo = person / "portrait.jpg"
    photo.write_bytes(b"jpeg-data")
    monkeypatch.setattr(web_gui, "_resolved_people_root", lambda: root)
    return root, photo


def test_delete_person_gallery_file_removes_file(people_env: tuple[Path, Path]) -> None:
    _root, photo = people_env
    result = web_gui._delete_person_gallery_file("Anna", str(photo))
    assert result["ok"] is True
    assert not photo.exists()


def test_delete_rejects_path_outside_person_folder(
    people_env: tuple[Path, Path], tmp_path: Path
) -> None:
    root, _photo = people_env
    other_person = root / "Bob"
    other_person.mkdir()
    other_file = other_person / "x.jpg"
    other_file.write_bytes(b"x")
    result = web_gui._delete_person_gallery_file("Anna", str(other_file))
    assert result["ok"] is False
    assert other_file.exists()


def test_delete_missing_file(people_env: tuple[Path, Path]) -> None:
    _root, photo = people_env
    photo.unlink()
    result = web_gui._delete_person_gallery_file("Anna", str(photo))
    assert result["ok"] is False
