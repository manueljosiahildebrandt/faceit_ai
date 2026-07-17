"""Identity matching: best cosine match across all stored embeddings per person."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from faceit_ai.persistence.repository import StoredEmbedding
from faceit_ai.settings import MatchingSettings


@dataclass(frozen=True)
class MatchResult:
    person_id: int | None
    person_name: str | None
    score: float
    is_unknown: bool
    needs_review_uncertain: bool


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """
    Cosine similarity in [-1, 1]. Always L2-normalizes in numerator/denominator so
    InsightFace vectors still compare correctly even if one side lost scale (DB, RAW path, etc.).
    """
    x = np.asarray(a, dtype=np.float64).ravel()
    y = np.asarray(b, dtype=np.float64).ravel()
    nx = float(np.linalg.norm(x))
    ny = float(np.linalg.norm(y))
    if nx < 1e-12 or ny < 1e-12:
        return 0.0
    c = float(np.dot(x, y) / (nx * ny))
    return float(np.clip(c, -1.0, 1.0))


def match_embedding(
    query: np.ndarray,
    gallery: list[StoredEmbedding],
    thresholds: MatchingSettings,
) -> MatchResult:
    if not gallery:
        return MatchResult(
            person_id=None, person_name=None, score=0.0, is_unknown=True, needs_review_uncertain=False
        )

    # Start below [-1, 1] so the first finite similarity always wins; avoids `-1.0 > -1.0` never
    # updating when every gallery match is exactly -1 or when floats are degenerate.
    best_score = -np.inf
    winner: StoredEmbedding | None = None
    for item in gallery:
        s = cosine_similarity(query, item.vector)
        if not np.isfinite(s):
            continue
        if s > best_score:
            best_score = s
            winner = item

    if winner is None:
        return MatchResult(
            person_id=None, person_name=None, score=0.0, is_unknown=True, needs_review_uncertain=False
        )

    cosine = float(best_score)
    scaled = cosine * float(thresholds.match_score_scale)
    t_rev = float(thresholds.match_threshold_review)
    t_strong = float(thresholds.match_threshold_strong)

    if scaled < t_rev:
        return MatchResult(
            person_id=None,
            person_name=None,
            score=scaled,
            is_unknown=True,
            needs_review_uncertain=False,
        )

    if scaled < t_strong:
        return MatchResult(
            person_id=winner.person_id,
            person_name=winner.person_name,
            score=scaled,
            is_unknown=False,
            needs_review_uncertain=True,
        )

    return MatchResult(
        person_id=winner.person_id,
        person_name=winner.person_name,
        score=scaled,
        is_unknown=False,
        needs_review_uncertain=False,
    )
