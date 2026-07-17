from __future__ import annotations

import numpy as np

from faceit_ai.vision.face_crop import (
    crop_bgr_to_portrait,
    parse_bbox_json,
    portrait_crop_rect,
    PortraitCropParams,
)


def test_parse_bbox_json() -> None:
    assert parse_bbox_json("[10, 20, 110, 120]") == (10.0, 20.0, 110.0, 120.0)


def test_portrait_crop_rect_clamps_to_image() -> None:
    # Face near top-left; crop should stay inside 200x300 image.
    x1, y1, x2, y2 = portrait_crop_rect(
        (5, 5, 55, 65),
        200,
        300,
        aspect_w=3,
        aspect_h=4,
        padding=1.5,
    )
    assert x1 >= 0
    assert y1 >= 0
    assert x2 <= 200
    assert y2 <= 300
    assert x2 > x1
    assert y2 > y1


def test_portrait_crop_rect_aspect_ratio() -> None:
    x1, y1, x2, y2 = portrait_crop_rect(
        (100, 100, 200, 220),
        800,
        600,
        aspect_w=3,
        aspect_h=4,
        padding=1.5,
    )
    w = x2 - x1
    h = y2 - y1
    assert abs((w / h) - (3 / 4)) < 0.05


def test_crop_bgr_to_portrait_returns_array() -> None:
    bgr = np.zeros((400, 300, 3), dtype=np.uint8)
    bgr[120:280, 80:220] = 255
    out = crop_bgr_to_portrait(bgr, (80, 120, 220, 280), PortraitCropParams())
    assert out.ndim == 3
    assert out.shape[2] == 3
    assert out.size > 0
