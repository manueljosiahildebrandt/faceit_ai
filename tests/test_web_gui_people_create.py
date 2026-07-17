"""People create/update API tests."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from faceit_ai import web_gui
from faceit_ai.multipart_form import parse_multipart
from faceit_ai.persistence.models import Base, Person
from faceit_ai.services.person_profile import read_person_json


def _multipart_fs(fields: dict[str, str], files: list[tuple[str, str, bytes]] | None = None):
    boundary = "----testboundary"
    body = BytesIO()
    for k, v in fields.items():
        body.write(f"--{boundary}\r\n".encode())
        body.write(f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode())
        body.write(f"{v}\r\n".encode())
    for field_name, filename, data in files or []:
        body.write(f"--{boundary}\r\n".encode())
        body.write(
            f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n'.encode()
        )
        body.write(b"Content-Type: image/jpeg\r\n\r\n")
        body.write(data)
        body.write(b"\r\n")
    body.write(f"--{boundary}--\r\n".encode())
    return parse_multipart(body.getvalue(), f"multipart/form-data; boundary={boundary}")


@pytest.fixture
def people_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    people = tmp_path / "people"
    people.mkdir()
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        f"database:\n  url: sqlite:///:memory:\npaths:\n  people_dir: {people}\n",
        encoding="utf-8",
    )
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sf = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(web_gui.STATE, "config_path", cfg_path)
    monkeypatch.setattr(web_gui.STATE, "people_root_last", str(people))
    monkeypatch.setattr(
        web_gui,
        "create_engine_and_session_factory",
        lambda _url: (engine, sf),
    )
    monkeypatch.setattr(web_gui, "_resolved_people_root", lambda: people)
    return people


def test_create_person_request(people_env: Path) -> None:
    fs = _multipart_fs(
        {
            "first_name": "Anna Maria",
            "last_name": "Mueller",
            "consent": "blocked",
        },
        files=[("photos", "portrait.jpg", b"fake-jpeg")],
    )
    resp = web_gui._create_person_request(fs)
    assert resp["ok"] is True
    slug = str(resp["slug"])
    assert slug == "mueller_anna-maria"
    folder = people_env / slug
    assert folder.is_dir()
    assert (folder / "portrait.jpg").is_file()
    profile = read_person_json(folder)
    assert profile is not None
    assert profile.display_name == "Anna Maria Mueller"


def test_create_person_rejects_existing_folder(people_env: Path) -> None:
    (people_env / "Ehmer_Daniel").mkdir()
    fs = _multipart_fs({"first_name": "Daniel", "last_name": "Ehmer", "consent": "blocked"})
    resp = web_gui._create_person_request(fs)
    assert resp["ok"] is False
    assert "already exists" in str(resp["error"]).lower()
    assert "Ehmer_Daniel" in str(resp["error"])


def test_update_person_tags(people_env: Path) -> None:
    fs = _multipart_fs({"first_name": "Daniel", "last_name": "Ehmer", "consent": "blocked"})
    create = web_gui._create_person_request(fs)
    slug = str(create["slug"])
    resp = web_gui._update_person_tags_request({"name": slug, "add": "2026,2025"})
    assert resp["ok"] is True
    assert resp["tags"] == [
        {"tag": "2025", "consent": "blocked"},
        {"tag": "2026", "consent": "blocked"},
    ]
    settings = web_gui.load_settings()
    _, session_factory = web_gui.create_engine_and_session_factory(settings.database_url)
    with web_gui.session_scope(session_factory) as session:
        row = session.scalar(select(Person).where(Person.name == slug))
        assert row is not None
        assert "2026" in str(row.tags_json)


def test_cycle_person_tag_consent(people_env: Path) -> None:
    fs = _multipart_fs({"first_name": "Daniel", "last_name": "Ehmer", "consent": "blocked"})
    create = web_gui._create_person_request(fs)
    slug = str(create["slug"])
    web_gui._update_person_tags_request({"name": slug, "add": "2026"})
    resp = web_gui._update_person_tags_request({"name": slug, "cycle": "2026"})
    assert resp["ok"] is True
    assert resp["tags"] == [{"tag": "2026", "consent": "allowed"}]


def test_ensure_person_folder_creates_missing(people_env: Path) -> None:
    resp = web_gui._ensure_person_folder_request("mueller_anna", "Anna Mueller")
    assert resp["ok"] is True
    assert resp["created"] is True
    assert resp["slug"] == "mueller_anna"
    folder = people_env / "mueller_anna"
    assert folder.is_dir()
    profile = read_person_json(folder)
    assert profile is not None
    assert "Anna" in (profile.display_name or "")


def test_ensure_person_folder_idempotent(people_env: Path) -> None:
    first = web_gui._ensure_person_folder_request("ehmer_daniel")
    assert first["ok"] is True
    second = web_gui._ensure_person_folder_request("ehmer_daniel")
    assert second["ok"] is True
    assert second["created"] is False
    assert second["slug"] == "ehmer_daniel"
