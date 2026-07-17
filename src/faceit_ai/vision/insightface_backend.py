"""
InsightFace wrapper: SCRFD detection + ArcFace embedding in one pass.

We set INSIGHTFACE_ROOT before importing FaceAnalysis so model cache location
is configurable and deployment can pre-seed models for air-gapped use.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from faceit_ai.inference.providers import device_kind, require_healthy_onnxruntime
from faceit_ai.settings import InsightFaceSettings


@dataclass(frozen=True)
class FaceDetectionResult:
    """One face with detector score, bbox, landmarks, and 512-d ArcFace embedding."""

    bbox_xyxy: tuple[float, float, float, float]
    det_score: float
    embedding: np.ndarray  # float32, L2-normalized by InsightFace


class InsightFaceBackend:
    """Lazy-loaded to avoid import side effects until first use."""

    def __init__(self, root: Path, cfg: InsightFaceSettings) -> None:
        self._root = root
        self._cfg = cfg
        self._providers: list[str] = list(cfg.providers)
        self._app: Any = None

    @property
    def embedding_dim(self) -> int:
        self._ensure()
        # buffalo_l recognizer is 512-d
        return 512

    def _build_app(self, providers: list[str]) -> Any:
        require_healthy_onnxruntime()
        os.environ["INSIGHTFACE_ROOT"] = str(self._root)
        from insightface.app import FaceAnalysis

        app = FaceAnalysis(
            name=self._cfg.model_name,
            root=str(self._root),
            providers=providers,
        )
        app.prepare(ctx_id=0, det_size=self._cfg.det_size)
        return app

    def _log_device(self, *, fallback: bool = False) -> None:
        import logging

        kind = device_kind(self._providers)
        providers = ", ".join(self._providers)
        log = logging.getLogger("faceit_ai")
        if fallback:
            log.warning(
                "InsightFace inference device: %s (providers: %s) — after provider fallback",
                kind,
                providers,
            )
        else:
            log.info(
                "InsightFace inference device: %s (providers: %s)",
                kind,
                providers,
            )

    def _ensure(self) -> None:
        if self._app is not None:
            return
        self._log_device()
        self._app = self._build_app(self._providers)

    def _fallback_to_cpu(self, err: BaseException) -> bool:
        """Rebuild on CPU after a CoreML/provider runtime failure. Returns True if rebuilt."""
        if self._providers == ["CPUExecutionProvider"]:
            return False
        import logging

        logging.getLogger("faceit_ai").warning(
            "InsightFace provider %s failed (%s); falling back to CPU.",
            ", ".join(self._providers),
            err,
        )
        self._providers = ["CPUExecutionProvider"]
        self._app = self._build_app(self._providers)
        self._log_device(fallback=True)
        return True

    def analyze(self, image_bgr: np.ndarray) -> list[FaceDetectionResult]:
        self._ensure()
        try:
            faces = self._app.get(image_bgr)
        except Exception as e:
            if not self._fallback_to_cpu(e):
                raise
            faces = self._app.get(image_bgr)
        out: list[FaceDetectionResult] = []
        for f in faces:
            bbox = f.bbox.astype(float)
            x1, y1, x2, y2 = float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3])
            emb = np.asarray(f.embedding, dtype=np.float32).reshape(-1)
            out.append(
                FaceDetectionResult(
                    bbox_xyxy=(x1, y1, x2, y2),
                    det_score=float(f.det_score),
                    embedding=emb,
                )
            )
        return out
