"""Person profile JSON + folder slug tests."""

from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from faceit_ai.persistence.models import Base, Person
from faceit_ai.services.person_profile import (
    PersonProfile,
    PersonTag,
    add_tags,
    cycle_tag_consent,
    existing_person_folder,
    folder_slug,
    merge_tags,
    parse_tags_raw,
    profile_for_folder,
    read_person_json,
    remove_tags,
    sync_person_profile_to_db,
    write_person_json,
)


def test_folder_slug_spaces_to_hyphen() -> None:
    assert folder_slug("Ehmer", "Anna Maria") == "ehmer_anna-maria"


def test_folder_slug_folds_german_umlauts() -> None:
    assert folder_slug("Müller", "Jörg") == "mueller_joerg"
    assert folder_slug("Weiß", "Änne") == "weiss_aenne"
    assert folder_slug("Öztürk", "Günther") == "oeztuerk_guenther"


def test_existing_person_folder(tmp_path: Path) -> None:
    root = tmp_path / "people"
    root.mkdir()
    (root / "Mueller_Anna").mkdir()
    assert existing_person_folder(root, "mueller_anna") == "Mueller_Anna"
    assert existing_person_folder(root, "other_person") is None


def test_parse_tags_legacy_strings() -> None:
    tags = parse_tags_raw(["2026", "2025"])
    assert [t.to_dict() for t in tags] == [
        {"tag": "2025", "consent": "blocked"},
        {"tag": "2026", "consent": "blocked"},
    ]


def test_person_json_round_trip(tmp_path: Path) -> None:
    folder = tmp_path / "Ehmer_Daniel"
    profile = PersonProfile(
        first_name="Daniel",
        last_name="Ehmer",
        display_name="Daniel Ehmer",
        tags=[
            PersonTag(tag="2026", consent="allowed"),
            PersonTag(tag="2025", consent="blocked"),
        ],
    )
    write_person_json(folder, profile)
    loaded = read_person_json(folder)
    assert loaded is not None
    assert loaded.display_name == "Daniel Ehmer"
    assert [t.to_dict() for t in loaded.tags] == [
        {"tag": "2025", "consent": "blocked"},
        {"tag": "2026", "consent": "allowed"},
    ]


def test_merge_tags_adds_blocked() -> None:
    p = PersonProfile(tags=[PersonTag(tag="2025", consent="allowed")])
    merge_tags(p, add=["2026"], remove=[])
    assert [t.to_dict() for t in p.tags] == [
        {"tag": "2025", "consent": "allowed"},
        {"tag": "2026", "consent": "blocked"},
    ]


def test_cycle_tag_consent() -> None:
    p = PersonProfile(tags=[PersonTag(tag="2026", consent="blocked")])
    cycle_tag_consent(p, "2026")
    assert p.tags[0].consent == "allowed"
    cycle_tag_consent(p, "2026")
    assert p.tags[0].consent == "none"
    cycle_tag_consent(p, "2026")
    assert p.tags[0].consent == "blocked"


def test_add_and_remove_tags() -> None:
    p = PersonProfile()
    add_tags(p, ["2026"])
    assert p.tags[0].consent == "blocked"
    remove_tags(p, ["2026"])
    assert p.tags == []


def test_sync_person_profile_to_db(tmp_path: Path) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sf = sessionmaker(engine, expire_on_commit=False)
    folder = tmp_path / "Ehmer_Daniel"
    write_person_json(
        folder,
        PersonProfile(first_name="Daniel", last_name="Ehmer", tags=[PersonTag(tag="2026")]),
    )
    with sf() as session:
        session.add(Person(name="Ehmer_Daniel", active=True))
        session.commit()
        sync_person_profile_to_db(session, folder_name="Ehmer_Daniel", folder=folder)
        session.commit()
        row = session.scalar(select(Person).where(Person.name == "Ehmer_Daniel"))
    assert row is not None
    assert row.display_name == "Daniel Ehmer"
    assert json.loads(str(row.tags_json)) == [{"tag": "2026", "consent": "blocked"}]


def test_profile_for_folder_migrates_legacy_tags(tmp_path: Path) -> None:
    folder = tmp_path / "Ehmer_Daniel"
    folder.mkdir()
    (folder / "person.json").write_text(
        json.dumps({"first_name": "Daniel", "last_name": "Ehmer", "tags": ["2026", "2025"]}),
        encoding="utf-8",
    )
    profile = profile_for_folder(folder, "Ehmer_Daniel")
    assert [t.to_dict() for t in profile.tags] == [
        {"tag": "2025", "consent": "blocked"},
        {"tag": "2026", "consent": "blocked"},
    ]
    migrated = json.loads((folder / "person.json").read_text(encoding="utf-8"))
    assert migrated["tags"] == [
        {"tag": "2025", "consent": "blocked"},
        {"tag": "2026", "consent": "blocked"},
    ]


def test_profile_for_folder_legacy(tmp_path: Path) -> None:
    folder = tmp_path / "Ehmer_Daniel"
    folder.mkdir()
    p = profile_for_folder(folder, "Ehmer_Daniel")
    assert "Ehmer" in p.display_name
