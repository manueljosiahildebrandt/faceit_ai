"""Load and normalize images for the detector (BGR uint8 for OpenCV / InsightFace)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from faceit_ai.settings import ImagePipelineSettings


class ImageDecodeError(Exception):
    """Raised when a file cannot be decoded (missing, corrupt, or unsupported RAW)."""

    def __init__(self, path: Path, message: str) -> None:
        self.path = path
        super().__init__(f"{message}: {path}")


@dataclass(frozen=True)
class LoadedImage:
    """BGR image as used by InsightFace; `path` is absolute for stable logging."""

    path: Path
    bgr: np.ndarray

    @property
    def shape(self) -> tuple[int, ...]:
        return self.bgr.shape


def _decode_raw_bgr(path: Path, *, half_size: bool) -> np.ndarray:
    """Decode camera RAW to BGR uint8 via LibRaw (rawpy)."""
    try:
        import rawpy
    except ImportError as e:
        raise ImportError(
            "RAW files require the 'rawpy' package. Install dependencies: pip install -e ."
        ) from e
    try:
        with rawpy.imread(str(path)) as raw:
            rgb = raw.postprocess(
                use_camera_wb=True,
                half_size=half_size,
                no_auto_bright=False,
                output_bps=8,
            )
    except Exception as e:
        hint = (
            "LibRaw may not support this camera/firmware yet, or the file is not a real RAW "
            "(e.g. truncated export). Try opening in Lightroom/Camera Raw, re-copy from card, "
            "or update rawpy. Or remove misnamed files from the folder."
        )
        raise ImageDecodeError(path, f"RAW decode failed ({e!r}). {hint}") from e
    return cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)


def _raw_decode_plan(decode_size: str) -> tuple[bool, float]:
    """Return (rawpy half_size flag, post-decode scale factor)."""
    if decode_size == "quarter":
        return True, 0.5
    if decode_size == "half":
        return True, 1.0
    return False, 1.0


def load_image_bgr(
    path: Path,
    max_dimension: int,
    *,
    raw_extensions: frozenset[str] | None = None,
    raw_decode_size: str = "full",
) -> LoadedImage:
    p = path.expanduser().resolve()
    suf = p.suffix.lower()
    raw_set = raw_extensions or frozenset()
    if suf in raw_set:
        half_size, post_scale = _raw_decode_plan(raw_decode_size)
        arr = _decode_raw_bgr(p, half_size=half_size)
        if post_scale != 1.0:
            h, w = arr.shape[:2]
            arr = cv2.resize(
                arr,
                (max(1, int(w * post_scale)), max(1, int(h * post_scale))),
                interpolation=cv2.INTER_AREA,
            )
    else:
        arr = cv2.imread(str(p), cv2.IMREAD_COLOR)
        if arr is None:
            raise ImageDecodeError(p, "OpenCV could not decode raster (empty or unsupported)")
    h, w = arr.shape[:2]
    m = max(h, w)
    if m > max_dimension and max_dimension > 0:
        scale = max_dimension / float(m)
        arr = cv2.resize(arr, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
    return LoadedImage(path=p, bgr=arr)


def load_image_for_pipeline(path: Path, cfg: ImagePipelineSettings) -> LoadedImage:
    """Decode using raster (OpenCV) or RAW (rawpy) based on `cfg` suffix lists."""
    return load_image_bgr(
        path,
        cfg.max_dimension,
        raw_extensions=frozenset(cfg.raw_extensions),
        raw_decode_size=cfg.raw_decode_size,
    )


def path_matches_ignore_rules(path: Path, ignore_filename_substrings: tuple[str, ...]) -> bool:
    """True if this file should be skipped (never decoded)."""
    if not ignore_filename_substrings:
        return False
    lower = path.name.lower()
    return any(s.lower() in lower for s in ignore_filename_substrings)


def path_is_under_flagged_tree(
    path: Path,
    scan_root: Path,
    *,
    flagged_dirname: str = "flagged",
) -> bool:
    """True when ``path`` lies under ``<scan_root>/<flagged_dirname>/``."""
    try:
        rel = path.expanduser().resolve().relative_to(scan_root.expanduser().resolve())
    except ValueError:
        return False
    return len(rel.parts) >= 1 and rel.parts[0] == flagged_dirname


def list_scannable_image_paths(
    folder: Path,
    *,
    extensions: tuple[str, ...],
    ignore_filename_substrings: tuple[str, ...],
    exclude_flagged_subtree: bool = False,
    flagged_dirname: str = "flagged",
) -> list[Path]:
    """All files under folder matching extensions, excluding ignored name patterns."""
    root = folder.expanduser().resolve()
    out: list[Path] = []
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in extensions:
            continue
        if path_matches_ignore_rules(p, ignore_filename_substrings):
            continue
        if exclude_flagged_subtree and path_is_under_flagged_tree(
            p, root, flagged_dirname=flagged_dirname
        ):
            continue
        out.append(p)
    return out


def file_digest_sha256(path: Path) -> str:
    import hashlib

    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()
