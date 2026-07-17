"""Person folder metadata: person.json on disk + DB sync."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from faceit_ai.persistence.models import Person

PERSON_JSON_FILENAME = "person.json"
PROFILE_VERSION = 1

TagConsent = Literal["blocked", "allowed", "none"]
TAG_CONSENT_CYCLE: tuple[TagConsent, ...] = ("blocked", "allowed", "none")

_UNSAFE_SLUG_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')


@dataclass
class PersonTag:
    tag: str
    consent: TagConsent = "blocked"

    def to_dict(self) -> dict[str, str]:
        return {"tag": self.tag, "consent": self.consent}

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> PersonTag | None:
        tag = str(data.get("tag") or "").strip()
        if not tag:
            return None
        return cls(tag=tag, consent=normalize_tag_consent(str(data.get("consent") or "blocked")))


def normalize_tag_consent(value: str) -> TagConsent:
    v = value.strip().lower()
    if v in TAG_CONSENT_CYCLE:
        return v  # type: ignore[return-value]
    return "blocked"


def parse_tags_raw(tags_raw: object) -> list[PersonTag]:
    """Parse tags from JSON — legacy plain strings migrate to blocked."""
    if not isinstance(tags_raw, list):
        return []
    by_key: dict[str, PersonTag] = {}
    for item in tags_raw:
        if isinstance(item, str):
            tag = item.strip()
            if not tag:
                continue
            key = tag.lower()
            if key not in by_key:
                by_key[key] = PersonTag(tag=tag, consent="blocked")
        elif isinstance(item, dict):
            parsed = PersonTag.from_dict(item)
            if parsed is None:
                continue
            by_key[parsed.tag.lower()] = parsed
    return sorted(by_key.values(), key=lambda t: t.tag.lower())


def tag_labels(tags: list[PersonTag]) -> list[str]:
    return [t.tag for t in tags]


@dataclass
class PersonProfile:
    first_name: str = ""
    last_name: str = ""
    display_name: str = ""
    tags: list[PersonTag] = field(default_factory=list)
    version: int = PROFILE_VERSION

    def to_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "first_name": self.first_name,
            "last_name": self.last_name,
            "display_name": self.display_name,
            "tags": [t.to_dict() for t in self.tags],
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> PersonProfile:
        first = str(data.get("first_name") or "").strip()
        last = str(data.get("last_name") or "").strip()
        display = str(data.get("display_name") or "").strip()
        if not display and (first or last):
            display = default_display_name(first, last)
        return cls(
            first_name=first,
            last_name=last,
            display_name=display,
            tags=parse_tags_raw(data.get("tags")),
            version=int(data.get("version") or PROFILE_VERSION),
        )


def default_display_name(first_name: str, last_name: str) -> str:
    parts = [p for p in (first_name.strip(), last_name.strip()) if p]
    return " ".join(parts)


def slug_part(text: str) -> str:
    """Sanitize one segment of a folder slug (lowercase)."""
    t = _UNSAFE_SLUG_RE.sub("", text.strip())
    t = re.sub(r"\s+", "-", t)
    t = t.strip(". ").lower()
    return t


def folder_slug(nachname: str, vorname: str) -> str:
    """Build folder slug: nachname_vorname (spaces in Vorname -> hyphens, all lowercase)."""
    last = slug_part(nachname)
    first = slug_part(vorname.replace(" ", "-"))
    if not last or not first:
        raise ValueError("Nachname and Vorname are required.")
    return f"{last}_{first}"


def existing_person_folder(root: Path, slug: str) -> str | None:
    """Return existing folder basename if slug conflicts (case-insensitive)."""
    if not root.is_dir():
        return None
    target = slug.lower()
    for entry in root.iterdir():
        if entry.is_dir() and entry.name.strip().lower() == target:
            return entry.name
    return None


def display_name_from_slug(slug: str) -> str:
    """Fallback display label for legacy folders without person.json."""
    return slug.replace("_", " ").replace("-", " ").strip() or slug


def person_json_path(folder: Path) -> Path:
    return folder / PERSON_JSON_FILENAME


def read_person_json(folder: Path) -> PersonProfile | None:
    path = person_json_path(folder)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return PersonProfile.from_dict(data)


def write_person_json(folder: Path, profile: PersonProfile) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    if not profile.display_name:
        profile.display_name = default_display_name(profile.first_name, profile.last_name)
    profile.tags = sorted(profile.tags, key=lambda t: t.tag.lower())
    person_json_path(folder).write_text(
        json.dumps(profile.to_dict(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _json_has_legacy_tags(data: dict[str, object]) -> bool:
    tags_raw = data.get("tags")
    if not isinstance(tags_raw, list):
        return False
    return any(isinstance(t, str) for t in tags_raw)


def profile_for_folder(folder: Path, folder_name: str) -> PersonProfile:
    """Load profile from JSON or infer from folder name."""
    path = person_json_path(folder)
    loaded = read_person_json(folder)
    if loaded is not None:
        if not loaded.display_name:
            loaded.display_name = display_name_from_slug(folder_name)
        if path.is_file():
            try:
                raw_data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                raw_data = None
            if isinstance(raw_data, dict) and _json_has_legacy_tags(raw_data):
                write_person_json(folder, loaded)
        return loaded
    return PersonProfile(display_name=display_name_from_slug(folder_name))


def tags_to_json(tags: list[PersonTag]) -> str:
    ordered = sorted(tags, key=lambda t: t.tag.lower())
    return json.dumps([t.to_dict() for t in ordered], ensure_ascii=False)


def tags_from_json(raw: str | None) -> list[PersonTag]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return parse_tags_raw(data)


def tags_to_dicts(tags: list[PersonTag]) -> list[dict[str, str]]:
    return [t.to_dict() for t in tags]


def sync_person_profile_to_db(
    session: Session,
    *,
    folder_name: str,
    folder: Path,
) -> Person | None:
    """Merge person.json into the Person row (by folder slug / Person.name)."""
    profile = profile_for_folder(folder, folder_name)
    person = session.scalar(
        select(Person).where(Person.name == folder_name).order_by(Person.active.desc())
    )
    if person is None:
        return None
    person.first_name = profile.first_name or person.first_name
    person.last_name = profile.last_name or person.last_name
    person.display_name = profile.display_name or display_name_from_slug(folder_name)
    person.tags_json = tags_to_json(profile.tags)
    session.flush()
    return person


def apply_profile_to_person(person: Person, profile: PersonProfile) -> None:
    person.first_name = profile.first_name
    person.last_name = profile.last_name
    person.display_name = profile.display_name or default_display_name(
        profile.first_name, profile.last_name
    )
    person.tags_json = tags_to_json(profile.tags)


def _find_tag(profile: PersonProfile, tag_name: str) -> PersonTag | None:
    key = tag_name.strip().lower()
    for t in profile.tags:
        if t.tag.lower() == key:
            return t
    return None


def add_tags(profile: PersonProfile, names: list[str], *, consent: TagConsent = "blocked") -> PersonProfile:
    existing = {t.tag.lower() for t in profile.tags}
    for name in names:
        n = name.strip()
        if not n or n.lower() in existing:
            continue
        profile.tags.append(PersonTag(tag=n, consent=consent))
        existing.add(n.lower())
    profile.tags.sort(key=lambda t: t.tag.lower())
    return profile


def remove_tags(profile: PersonProfile, names: list[str]) -> PersonProfile:
    remove_keys = {n.strip().lower() for n in names if n.strip()}
    profile.tags = [t for t in profile.tags if t.tag.lower() not in remove_keys]
    return profile


def cycle_tag_consent(profile: PersonProfile, tag_name: str) -> PersonProfile:
    """Cycle consent: blocked → allowed → none → blocked."""
    tag = _find_tag(profile, tag_name)
    if tag is None:
        raise ValueError(f"Tag not found: {tag_name}")
    idx = TAG_CONSENT_CYCLE.index(tag.consent)
    tag.consent = TAG_CONSENT_CYCLE[(idx + 1) % len(TAG_CONSENT_CYCLE)]
    return profile


def merge_tags(profile: PersonProfile, add: list[str], remove: list[str]) -> PersonProfile:
    remove_tags(profile, remove)
    add_tags(profile, add, consent="blocked")
    return profile
