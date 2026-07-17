"""Settings form HTML helpers."""

from __future__ import annotations

from faceit_ai import web_gui


def test_label_with_help_keeps_mark_inline() -> None:
    html = web_gui._label_with_help(
        "Crop portraits for People folder",
        "Save face-centered portrait JPEGs.",
    )
    assert 'class="label-with-help"' in html
    assert "Crop portraits for People folder" in html
    assert 'class="col-help"' in html
    assert html.index("People folder") < html.index("col-help")


def test_label_with_help_no_tip() -> None:
    html = web_gui._label_with_help("Plain label")
    assert html == '<span class="label-with-help">Plain label</span>'


def test_settings_ai_intro_bilingual() -> None:
    en = web_gui._settings_ai_intro_html("en")
    de = web_gui._settings_ai_intro_html("de")
    assert "settings-ai-intro" in en
    assert "Too slow?" in en
    assert "Per photo:" in en
    assert "Zu langsam?" in de
    assert "Ablauf pro Foto" in de
