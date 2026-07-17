"""Unit tests for decision logic (no ML imports)."""

from __future__ import annotations

from datetime import UTC, datetime

from faceit_ai.decision.engine import FaceDecisionInput, decide_image
from faceit_ai.persistence.models import Consent
from faceit_ai.settings import DecisionSettings
from faceit_ai.vision.matcher import MatchResult


def _consent(**kwargs: bool) -> Consent:
    c = Consent(
        person_id=1,
        consent_given=kwargs.get("consent_given", True),
        usage_social=kwargs.get("usage_social", True),
        usage_web=kwargs.get("usage_web", True),
        usage_internal=kwargs.get("usage_internal", True),
        usage_print=kwargs.get("usage_print", True),
    )
    c.updated_at = datetime.now(UTC)
    return c


def test_no_faces_ok() -> None:
    cfg = DecisionSettings(min_confident_match=0.6)
    d = decide_image(
        face_inputs=[],
        consent_lookup={},
        usage_column="usage_social",
        decision_cfg=cfg,
    )
    assert d.status == "ok"
    assert d.reason == "no_faces"


def test_unknown_face_ok_by_default() -> None:
    cfg = DecisionSettings(min_confident_match=0.6, unknown_face_status="ok")
    fi = FaceDecisionInput(
        match=MatchResult(
            person_id=None,
            person_name=None,
            score=0.1,
            is_unknown=True,
            needs_review_uncertain=False,
        )
    )
    d = decide_image(
        face_inputs=[fi],
        consent_lookup={},
        usage_column="usage_social",
        decision_cfg=cfg,
    )
    assert d.status == "ok"
    assert d.reason == "all_clear"


def test_unknown_face_review_when_configured() -> None:
    cfg = DecisionSettings(min_confident_match=0.6, unknown_face_status="review")
    fi = FaceDecisionInput(
        match=MatchResult(
            person_id=None,
            person_name=None,
            score=0.1,
            is_unknown=True,
            needs_review_uncertain=False,
        )
    )
    d = decide_image(
        face_inputs=[fi],
        consent_lookup={},
        usage_column="usage_social",
        decision_cfg=cfg,
    )
    assert d.status == "review"
    assert d.reason == "unknown_face"


def test_no_consent_blocked() -> None:
    cfg = DecisionSettings(min_confident_match=0.6)
    fi = FaceDecisionInput(
        match=MatchResult(
            person_id=1,
            person_name="Max",
            score=0.95,
            is_unknown=False,
            needs_review_uncertain=False,
        )
    )
    d = decide_image(
        face_inputs=[fi],
        consent_lookup={1: _consent(consent_given=False)},
        usage_column="usage_social",
        decision_cfg=cfg,
    )
    assert d.status == "blocked"
    assert d.reason == "no_consent"


def test_usage_not_allowed_blocked() -> None:
    cfg = DecisionSettings(min_confident_match=0.6)
    fi = FaceDecisionInput(
        match=MatchResult(
            person_id=1,
            person_name="Max",
            score=0.95,
            is_unknown=False,
            needs_review_uncertain=False,
        )
    )
    d = decide_image(
        face_inputs=[fi],
        consent_lookup={1: _consent(usage_social=False)},
        usage_column="usage_social",
        decision_cfg=cfg,
    )
    assert d.status == "blocked"
    assert d.reason == "usage_not_allowed"


def test_all_clear_ok() -> None:
    cfg = DecisionSettings(min_confident_match=0.6)
    fi = FaceDecisionInput(
        match=MatchResult(
            person_id=1,
            person_name="Max",
            score=0.95,
            is_unknown=False,
            needs_review_uncertain=False,
        )
    )
    d = decide_image(
        face_inputs=[fi],
        consent_lookup={1: _consent()},
        usage_column="usage_social",
        decision_cfg=cfg,
    )
    assert d.status == "ok"
    assert d.reason == "all_clear"


def test_uncertain_match_allowed_is_ok() -> None:
    """Weak match to an Allowed person is fine — not Review."""
    cfg = DecisionSettings(min_confident_match=0.6)
    fi = FaceDecisionInput(
        match=MatchResult(
            person_id=1,
            person_name="Max",
            score=150.0,
            is_unknown=False,
            needs_review_uncertain=True,
        )
    )
    d = decide_image(
        face_inputs=[fi],
        consent_lookup={1: _consent()},
        usage_column="usage_social",
        decision_cfg=cfg,
    )
    assert d.status == "ok"
    assert d.reason == "all_clear"


def test_uncertain_match_no_consent_is_review() -> None:
    """Weak match to a Blocked person → Review (might be them)."""
    cfg = DecisionSettings(min_confident_match=0.6)
    fi = FaceDecisionInput(
        match=MatchResult(
            person_id=1,
            person_name="Max",
            score=150.0,
            is_unknown=False,
            needs_review_uncertain=True,
        )
    )
    d = decide_image(
        face_inputs=[fi],
        consent_lookup={1: _consent(consent_given=False)},
        usage_column="usage_social",
        decision_cfg=cfg,
    )
    assert d.status == "review"
    assert d.reason == "possible_no_consent"


def test_blocked_overrides_review() -> None:
    cfg = DecisionSettings(min_confident_match=0.6)
    unknown = FaceDecisionInput(
        match=MatchResult(
            person_id=None,
            person_name=None,
            score=0.1,
            is_unknown=True,
            needs_review_uncertain=False,
        )
    )
    bad = FaceDecisionInput(
        match=MatchResult(
            person_id=1,
            person_name="Max",
            score=0.95,
            is_unknown=False,
            needs_review_uncertain=False,
        )
    )
    d = decide_image(
        face_inputs=[unknown, bad],
        consent_lookup={1: _consent(consent_given=False)},
        usage_column="usage_social",
        decision_cfg=cfg,
    )
    assert d.status == "blocked"
