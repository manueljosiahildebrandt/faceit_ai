"""RAW decode size config and loader plan."""

from __future__ import annotations

from faceit_ai.settings import parse_raw_decode_size
from faceit_ai.vision.image_loader import _raw_decode_plan


def test_parse_raw_decode_size_explicit() -> None:
    assert parse_raw_decode_size({"raw_decode_size": "quarter"}) == "quarter"
    assert parse_raw_decode_size({"raw_decode_size": "full"}) == "full"


def test_parse_raw_decode_size_legacy_bool() -> None:
    assert parse_raw_decode_size({"raw_half_size": True}) == "half"
    assert parse_raw_decode_size({"raw_half_size": False}) == "full"


def test_raw_decode_plan() -> None:
    assert _raw_decode_plan("full") == (False, 1.0)
    assert _raw_decode_plan("half") == (True, 1.0)
    assert _raw_decode_plan("quarter") == (True, 0.5)
