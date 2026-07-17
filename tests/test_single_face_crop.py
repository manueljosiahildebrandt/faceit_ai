"""Single-face portrait crop helper."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest

from faceit_ai.settings import CollectSettings
from faceit_ai.vision.single_face_crop import write_single_face_portrait


@dataclass
class _FakeFace:
    det_score: float
    bbox_xyxy: tuple[float, float, float, float] = (0, 0, 10, 10)


def test_write_single_face_portrait_tightens_until_one_face(tmp_path: Path, monkeypatch) -> None:
    src = tmp_path / "src.jpg"
    src.write_bytes(b"\xff\xd8\xff\xd9")
    dest = tmp_path / "out.jpg"
    bgr = np.zeros((100, 80, 3), dtype=np.uint8)

    calls: list[float] = []

    class FakeBackend:
        def analyze(self, crop):
            calls.append(1)
            # First attempt: two faces; second: one face.
            if len(calls) == 1:
                return [_FakeFace(0.9), _FakeFace(0.8)]
            return [_FakeFace(0.95)]

    monkeypatch.setattr(
        "faceit_ai.vision.single_face_crop.load_image_for_pipeline",
        lambda _p, _cfg: MagicMock(bgr=bgr),
    )
    monkeypatch.setattr(
        "faceit_ai.vision.single_face_crop.write_portrait_jpeg",
        lambda path, _bgr: Path(path).write_bytes(b"jpeg"),
    )

    ok = write_single_face_portrait(
        source_path=src,
        dest=dest,
        bbox=(20.0, 20.0, 60.0, 70.0),
        image_cfg=MagicMock(),
        collect=CollectSettings(people_root=tmp_path, crop_padding=1.5),
        backend=FakeBackend(),  # type: ignore[arg-type]
    )
    assert ok is True
    assert dest.is_file()
    assert len(calls) >= 2


def test_write_single_face_portrait_fails_when_still_multi_face(tmp_path: Path, monkeypatch) -> None:
    src = tmp_path / "src.jpg"
    src.write_bytes(b"\xff\xd8\xff\xd9")
    dest = tmp_path / "out.jpg"
    bgr = np.zeros((100, 80, 3), dtype=np.uint8)

    class FakeBackend:
        def analyze(self, crop):
            return [_FakeFace(0.9), _FakeFace(0.85)]

    monkeypatch.setattr(
        "faceit_ai.vision.single_face_crop.load_image_for_pipeline",
        lambda _p, _cfg: MagicMock(bgr=bgr),
    )
    wrote = {"n": 0}

    def _no_write(path, _bgr):
        wrote["n"] += 1

    monkeypatch.setattr("faceit_ai.vision.single_face_crop.write_portrait_jpeg", _no_write)

    ok = write_single_face_portrait(
        source_path=src,
        dest=dest,
        bbox=(20.0, 20.0, 60.0, 70.0),
        image_cfg=MagicMock(),
        collect=CollectSettings(people_root=tmp_path, crop_padding=1.2),
        backend=FakeBackend(),  # type: ignore[arg-type]
    )
    assert ok is False
    assert wrote["n"] == 0
