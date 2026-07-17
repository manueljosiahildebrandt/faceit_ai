"""Matcher edge cases (gallery non-empty but pathological scores)."""

from __future__ import annotations

import numpy as np

from faceit_ai.persistence.repository import StoredEmbedding
from faceit_ai.settings import MatchingSettings
from faceit_ai.vision.matcher import match_embedding


def test_best_match_when_all_cosines_are_minus_one() -> None:
    """Regression: initial best was -1.0 so s > -1.0 failed for s == -1.0 and winner stayed None."""
    q = np.array([1.0, 0.0], dtype=np.float32)
    g1 = np.array([-1.0, 0.0], dtype=np.float32)
    gallery = [
        StoredEmbedding(id=1, person_id=1, person_name="a", vector=g1),
    ]
    th = MatchingSettings(
        match_score_scale=1.0, match_threshold_strong=0.6, match_threshold_review=0.4
    )
    r = match_embedding(q, gallery, th)
    assert r.person_id is None
    assert r.score == -1.0
    assert r.is_unknown is True


def test_picks_highest_similarity_not_first_entry() -> None:
    q = np.array([1.0, 0.0], dtype=np.float32)
    v1 = np.array([0.0, 1.0], dtype=np.float32)  # cos=0 with q
    v2 = np.array([1.0, 0.0], dtype=np.float32)  # cos=1
    gallery = [
        StoredEmbedding(id=1, person_id=1, person_name="low", vector=v1),
        StoredEmbedding(id=2, person_id=2, person_name="best", vector=v2),
    ]
    th = MatchingSettings(
        match_score_scale=1.0, match_threshold_strong=0.6, match_threshold_review=0.4
    )
    r = match_embedding(q, gallery, th)
    assert r.person_id == 2
    assert abs(r.score - 1.0) < 1e-5
    assert r.is_unknown is False


def test_cosine_similarity_scale_invariant() -> None:
    from faceit_ai.vision.matcher import cosine_similarity

    q = np.array([1.0, 0.0], dtype=np.float32)
    g = np.array([400.0, 0.0], dtype=np.float32)
    assert abs(cosine_similarity(q, g) - 1.0) < 1e-5
