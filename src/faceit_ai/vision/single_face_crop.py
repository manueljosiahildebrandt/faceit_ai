"""Iterative portrait crop until exactly one face is detected in the crop."""

from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path

from faceit_ai.settings import CollectSettings, ImagePipelineSettings
from faceit_ai.vision.face_crop import (
    PortraitCropParams,
    crop_bgr_to_portrait,
    write_portrait_jpeg,
)
from faceit_ai.vision.image_loader import ImageDecodeError, load_image_for_pipeline
from faceit_ai.vision.insightface_backend import InsightFaceBackend

_MIN_PADDING = 1.05
_PADDING_STEP = 0.15
_MAX_ATTEMPTS = 8
_MIN_DET_SCORE = 0.5


def _count_confident_faces(backend: InsightFaceBackend, bgr) -> int:
    faces = backend.analyze(bgr)
    return sum(1 for f in faces if f.det_score >= _MIN_DET_SCORE)


def write_single_face_portrait(
    *,
    source_path: Path,
    dest: Path,
    bbox: tuple[float, float, float, float],
    image_cfg: ImagePipelineSettings,
    collect: CollectSettings,
    backend: InsightFaceBackend,
    log: logging.Logger | None = None,
) -> bool:
    """Crop around ``bbox``, tightening until the crop contains exactly one face.

    Returns True when a single-face JPEG was written.
    """
    lg = log or logging.getLogger("faceit_ai")
    try:
        loaded = load_image_for_pipeline(source_path, image_cfg)
    except ImageDecodeError as err:
        lg.warning("single_face_crop: decode failed for %s: %s", source_path, err)
        return False

    base_params = PortraitCropParams(
        aspect_w=collect.crop_aspect_w,
        aspect_h=collect.crop_aspect_h,
        padding=collect.crop_padding,
    )
    padding = float(collect.crop_padding)
    last_crop = None

    for _ in range(_MAX_ATTEMPTS):
        params = replace(base_params, padding=padding)
        cropped = crop_bgr_to_portrait(loaded.bgr, bbox, params)
        last_crop = cropped
        n_faces = _count_confident_faces(backend, cropped)
        if n_faces == 1:
            write_portrait_jpeg(dest, cropped)
            return True
        if n_faces == 0:
            lg.warning(
                "single_face_crop: no face at padding=%.2f for %s",
                padding,
                source_path,
            )
            return False
        if padding <= _MIN_PADDING + 1e-6:
            lg.warning(
                "single_face_crop: still %d faces at min padding for %s — skip",
                n_faces,
                source_path,
            )
            return False
        padding = max(_MIN_PADDING, padding - _PADDING_STEP)

    if last_crop is not None:
        n_faces = _count_confident_faces(backend, last_crop)
        if n_faces == 1:
            write_portrait_jpeg(dest, last_crop)
            return True
    lg.warning("single_face_crop: gave up after %d attempts for %s", _MAX_ATTEMPTS, source_path)
    return False
