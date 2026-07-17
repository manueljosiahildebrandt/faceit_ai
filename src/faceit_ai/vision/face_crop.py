"""Face-centered portrait crops for people-folder collect."""

from __future__ import annotations

import json
from dataclasses import dataclass

import cv2
import numpy as np


@dataclass(frozen=True)
class PortraitCropParams:
    aspect_w: float = 3.0
    aspect_h: float = 4.0
    padding: float = 1.5


def parse_bbox_json(text: str) -> tuple[float, float, float, float]:
    """Parse ``AssetFace.bbox`` JSON ``[x1,y1,x2,y2]``."""
    raw = json.loads(text)
    if not isinstance(raw, list) or len(raw) != 4:
        raise ValueError(f"expected bbox [x1,y1,x2,y2], got {text!r}")
    x1, y1, x2, y2 = (float(raw[0]), float(raw[1]), float(raw[2]), float(raw[3]))
    if x2 <= x1 or y2 <= y1:
        raise ValueError(f"degenerate bbox {text!r}")
    return x1, y1, x2, y2


def portrait_crop_rect(
    bbox: tuple[float, float, float, float],
    img_w: int,
    img_h: int,
    *,
    aspect_w: float,
    aspect_h: float,
    padding: float,
) -> tuple[int, int, int, int]:
    """Expand face bbox with padding, fit aspect ratio, center face, clamp to image."""
    x1, y1, x2, y2 = bbox
    fw = max(1.0, x2 - x1)
    fh = max(1.0, y2 - y1)
    pad = max(1.0, float(padding))
    cx = (x1 + x2) / 2.0
    cy = (y1 + y2) / 2.0

    # Padded box around the face (square-ish base from max dimension).
    half = max(fw, fh) * pad / 2.0
    crop_w = half * 2.0
    crop_h = half * 2.0

    aspect = float(aspect_w) / float(aspect_h) if aspect_h else 1.0
    if crop_w / crop_h > aspect:
        crop_h = crop_w / aspect
    else:
        crop_w = crop_h * aspect

    left = cx - crop_w / 2.0
    top = cy - crop_h / 2.0
    right = left + crop_w
    bottom = top + crop_h

    # Clamp while preserving size where possible.
    if left < 0:
        right -= left
        left = 0.0
    if top < 0:
        bottom -= top
        top = 0.0
    if right > img_w:
        shift = right - img_w
        left = max(0.0, left - shift)
        right = float(img_w)
    if bottom > img_h:
        shift = bottom - img_h
        top = max(0.0, top - shift)
        bottom = float(img_h)

    ix1 = int(max(0, min(img_w - 1, round(left))))
    iy1 = int(max(0, min(img_h - 1, round(top))))
    ix2 = int(max(ix1 + 1, min(img_w, round(right))))
    iy2 = int(max(iy1 + 1, min(img_h, round(bottom))))
    return ix1, iy1, ix2, iy2


def crop_bgr_to_portrait(
    bgr: np.ndarray,
    bbox: tuple[float, float, float, float],
    params: PortraitCropParams,
) -> np.ndarray:
    """Return a BGR crop from ``bgr`` using ``portrait_crop_rect``."""
    h, w = bgr.shape[:2]
    x1, y1, x2, y2 = portrait_crop_rect(
        bbox,
        w,
        h,
        aspect_w=params.aspect_w,
        aspect_h=params.aspect_h,
        padding=params.padding,
    )
    return bgr[y1:y2, x1:x2].copy()


def write_portrait_jpeg(path: str | Path, bgr: np.ndarray, *, quality: int = 92) -> None:
    """Write a BGR image as JPEG."""
    from pathlib import Path as _Path

    p = _Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    ok = cv2.imwrite(str(p), bgr, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
    if not ok:
        raise OSError(f"cv2.imwrite failed for {p}")
