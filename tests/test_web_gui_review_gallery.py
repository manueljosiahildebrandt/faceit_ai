"""Review gallery web API tests."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from faceit_ai import web_gui
from faceit_ai.persistence.models import Asset, AssetDecision, AssetFace, Base, Person


@pytest.fixture
def review_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, int]:
    shoot = tmp_path / "shoot"
    people = tmp_path / "people"
    shoot.mkdir()
    people.mkdir()
    photo = shoot / "review.jpg"
    photo.write_bytes(b"jpeg")

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sf = sessionmaker(engine, expire_on_commit=False)
    with sf() as s:
        person = Person(name="Tina", active=True)
        s.add(person)
        s.flush()
        asset = Asset(path=str(photo.resolve()), sha256="r1", processed_at=None)
        s.add(asset)
        s.flush()
        s.add(
            AssetFace(
                asset_id=asset.id,
                bbox="[20,30,80,90]",
                embedding=b"\x00" * (512 * 4),
                match_person_id=person.id,
                match_score=210.0,
            )
        )
        s.add(
            AssetDecision(
                asset_id=asset.id,
                status="review",
                reason="possible_no_consent",
                usage="social",
            )
        )
        s.commit()
        asset_id = int(asset.id)

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        f"database:\n  url: sqlite:///:memory:\npaths:\n  people_dir: {people}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(web_gui.STATE, "config_path", cfg_path)
    monkeypatch.setattr(
        web_gui,
        "create_engine_and_session_factory",
        lambda _url: (engine, sf),
    )
    monkeypatch.setattr(web_gui, "_resolved_people_root", lambda: people)
    return shoot, asset_id


def test_review_photos_response(review_env: tuple[Path, int]) -> None:
    shoot, asset_id = review_env
    resp = web_gui._review_photos_response(str(shoot))
    assert resp["ok"] is True
    assert resp["count"] == 1
    assert resp["status"] == "review"
    assert resp["status_counts"] == {"review": 1, "blocked": 0}
    photos = resp["photos"]
    assert isinstance(photos, list)
    assert photos[0]["asset_id"] == asset_id
    assert "preview_url" in photos[0]


def test_review_photos_response_blocked_empty(review_env: tuple[Path, int]) -> None:
    shoot, _asset_id = review_env
    resp = web_gui._review_photos_response(str(shoot), status="blocked")
    assert resp["ok"] is True
    assert resp["status"] == "blocked"
    assert resp["count"] == 0


def test_confirm_review_ok_request(review_env: tuple[Path, int]) -> None:
    shoot, asset_id = review_env
    resp = web_gui._confirm_review_ok_request(asset_id, str(shoot))
    assert resp["ok"] is True
    assert "OK" in str(resp.get("message", ""))


def test_confirm_blocked_ok_request(review_env: tuple[Path, int]) -> None:
    shoot, asset_id = review_env
    settings = web_gui.load_settings()
    _, session_factory = web_gui.create_engine_and_session_factory(settings.database_url)
    with web_gui.session_scope(session_factory) as session:
        from sqlalchemy import select

        from faceit_ai.persistence.models import AssetDecision

        dec = session.scalar(select(AssetDecision).where(AssetDecision.asset_id == asset_id))
        assert dec is not None
        dec.status = "blocked"
        dec.reason = "no_consent"
    resp = web_gui._confirm_blocked_ok_request(asset_id, str(shoot))
    assert resp["ok"] is True
    with web_gui.session_scope(session_factory) as session:
        from sqlalchemy import select

        from faceit_ai.persistence.models import AssetDecision

        dec2 = session.scalar(select(AssetDecision).where(AssetDecision.asset_id == asset_id))
        assert dec2 is not None
        assert dec2.status == "ok"
        assert dec2.reason == "cleared_from_blocked"


def test_review_photo_detail(review_env: tuple[Path, int]) -> None:
    shoot, asset_id = review_env
    resp = web_gui._review_photo_detail(asset_id, str(shoot))
    assert resp["ok"] is True
    assert resp["asset_id"] == asset_id
    assert len(resp["faces"]) == 1
    assert resp["faces"][0]["person_name"] == "Tina"


def test_confirm_rejects_invalid_folder(review_env: tuple[Path, int]) -> None:
    _shoot, asset_id = review_env
    resp = web_gui._confirm_review_blocked_request(
        asset_id,
        "/no/such/folder",
        '[{"face_id": 1, "person_name": "Tina"}]',
    )
    assert resp["ok"] is False


@patch("faceit_ai.services.review_confirm._write_cropped_portrait", return_value=True)
def test_batch_confirm_review_blocked_request(
    _mock_crop: object, review_env: tuple[Path, int]
) -> None:
    shoot, asset_id = review_env
    resp = web_gui._batch_confirm_review_blocked_request(str(shoot))
    assert resp["ok"] is True
    assert resp["moved"] == 1
    assert resp["skipped"] == 0
    assert resp["errors"] == 0
    settings = web_gui.load_settings()
    _, session_factory = web_gui.create_engine_and_session_factory(settings.database_url)
    with web_gui.session_scope(session_factory) as session:
        from sqlalchemy import select

        from faceit_ai.persistence.models import AssetDecision

        dec = session.scalar(select(AssetDecision).where(AssetDecision.asset_id == asset_id))
        assert dec is not None
        assert dec.status == "blocked"
