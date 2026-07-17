from __future__ import annotations

from faceit_ai.reporting import DecisionSummary, format_summary_text


def test_format_summary_text_empty() -> None:
    s = DecisionSummary(counts_by_status={}, total_assets_with_decision=0, sample_paths_by_status={})
    out = format_summary_text(s)
    assert "0" in out
    assert "Counts by status" in out
