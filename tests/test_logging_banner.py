"""ASCII run banners for console."""

from __future__ import annotations

from faceit_ai.logging_setup import run_banner_lines


def test_run_banner_lines_shape() -> None:
    top, mid, bot = run_banner_lines("HEADER", width=20)
    assert len(top) == 20 == len(bot)
    assert top == "-" * 20 == bot
    assert "HEADER" in mid
    assert len(mid) == 20


def test_run_banner_truncates_long_title() -> None:
    t, m, b = run_banner_lines("x" * 100, width=20)
    assert len(m) == 20
    assert "…" in m
