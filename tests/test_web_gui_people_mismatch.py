"""People table embedding mismatch warning."""

from __future__ import annotations

from faceit_ai import web_gui


def test_person_needs_reregister() -> None:
    assert web_gui._person_needs_reregister(146, 31) is True
    assert web_gui._person_needs_reregister(14, 14) is False
    assert web_gui._person_needs_reregister(0, 0) is False
    assert web_gui._person_needs_reregister(0, 5) is False


def test_mismatch_tooltip() -> None:
    assert web_gui._mismatch_tooltip(146, 31) == "146 photos, 31 embeddings — re-register needed."


def test_people_table_shows_mismatch_warning() -> None:
    rows = [
        {
            "name": "test_josia-test",
            "display_name": "Josia Test",
            "photos": 146,
            "embeddings": 31,
            "status": "Registered",
            "consent": False,
            "registered": True,
            "needs_reregister": True,
            "tags": [{"tag": "2026", "consent": "blocked"}],
            "first_name": "Josia",
            "last_name": "Test",
        }
    ]
    html = web_gui._people_table_body_html(rows)
    assert "Josia Test" in html
    assert "⚠️" in html
    assert "embedding-mismatch" in html
    assert "data-search-text" in html
    assert "data-sort-name" in html
    assert "tag-pill" in html
    assert "tag-consent-blocked" in html
    assert "consent-pill" in html
    assert web_gui._people_mismatch_warn_visible(rows) is True
    nav = web_gui._nav_people_link_html(active_cls="", mismatch=True)
    assert 'id="nav_people_link"' in nav
    assert "People" in nav
    assert "⚠️" in nav
    nav_hidden = web_gui._nav_people_link_html(active_cls="", mismatch=False)
    assert "display:none" in nav_hidden


def test_people_table_shows_status_under_name_when_not_registered() -> None:
    rows = [
        {
            "name": "new_person",
            "display_name": "New Person",
            "photos": 3,
            "embeddings": 0,
            "status": "Not registered",
            "consent": False,
            "registered": False,
            "needs_reregister": False,
            "tags": [],
            "first_name": "",
            "last_name": "",
        }
    ]
    html = web_gui._people_table_body_html(rows)
    assert "people-name-sub" in html
    assert "Not registered" in html
    assert "Register</button>" in html


def test_people_table_onclick_uses_safe_quotes() -> None:
    rows = [
        {
            "name": 'test"name',
            "photos": 10,
            "embeddings": 10,
            "status": "Registered",
            "consent": True,
            "registered": True,
            "needs_reregister": False,
        }
    ]
    html = web_gui._people_table_body_html(rows)
    assert "onclick='return openGallery(\"test\\\"name\")'" in html or "onclick='return openGallery(" in html
    assert 'onclick="return openGallery(' not in html
    assert "onclick='peopleConsentToggle(" in html


def test_tags_cell_cycle_uses_single_quoted_onclick() -> None:
    html = web_gui._tags_cell_html(
        '"ehmer_daniel"',
        [{"tag": "2026", "consent": "blocked"}],
        "ehmer_daniel",
    )
    assert "onclick='cycleTagConsent(event," in html
    assert 'onclick="cycleTagConsent' not in html
    assert "tag-consent-blocked" in html
    assert 'data-person-slug="ehmer_daniel"' in html
    assert "class='tag-body'" in html


def test_tags_cell_renders_picker_data_attrs() -> None:
    html = web_gui._tags_cell_html(
        '"person_a"',
        [{"tag": "2025", "consent": "allowed"}, {"tag": "2026", "consent": "none"}],
        "person_a",
    )
    assert "tag-consent-allowed" in html
    assert "tag-consent-none" in html
    assert 'class="tags-cell"' in html
    assert 'class="tags-cell-pills"' in html
