"""
Decision logic: matcher tiers (unknown / uncertain / strong) + consent.

Aggregate precedence:
1. Strong match to a Blocked / disallowed-usage person → blocked.
2. Uncertain match to a Blocked / disallowed-usage person → review
   (might be them — needs a human check).
3. Unknown faces follow ``decision.unknown_face_status`` (default ``ok``).
4. Matches to Allowed people (strong or uncertain) → ok.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from faceit_ai.persistence.models import Consent
from faceit_ai.settings import DecisionSettings
from faceit_ai.vision.matcher import MatchResult


@dataclass(frozen=True)
class FaceDecisionInput:
    match: MatchResult


@dataclass(frozen=True)
class AggregatedDecision:
    status: str  # blocked | review | ok
    reason: str
    faces_out: list[dict[str, Any]]


def _usage_allowed(consent: Consent | None, usage_key: str) -> bool:
    if consent is None:
        return False
    val = getattr(consent, usage_key, None)
    return bool(val)


def decide_image(
    *,
    face_inputs: list[FaceDecisionInput],
    consent_lookup: dict[int, Consent | None],
    usage_column: str,
    decision_cfg: DecisionSettings,
) -> AggregatedDecision:
    if not face_inputs:
        return AggregatedDecision(status="ok", reason="no_faces", faces_out=[])

    per_face_blocked: list[str] = []
    per_face_review: list[str] = []
    faces_out: list[dict[str, Any]] = []
    unknown_status = decision_cfg.unknown_face_status

    for fi in face_inputs:
        m = fi.match
        entry: dict[str, Any] = {
            "person": m.person_name,
            "confidence": round(m.score, 4),
        }

        if m.is_unknown:
            # Strangers: default ok. Optional review via config.
            if unknown_status == "review":
                per_face_review.append("unknown_face")
            faces_out.append(entry)
            continue

        assert m.person_id is not None
        consent = consent_lookup.get(m.person_id)
        disallowed = consent is None or not consent.consent_given
        usage_denied = (not disallowed) and (not _usage_allowed(consent, usage_column))

        if disallowed or usage_denied:
            # Review = "might be a blocked person"; Blocked = confident match.
            if m.needs_review_uncertain:
                per_face_review.append(
                    "possible_no_consent" if disallowed else "possible_usage_not_allowed"
                )
            else:
                per_face_blocked.append(
                    "no_consent" if disallowed else "usage_not_allowed"
                )
            faces_out.append(entry)
            continue

        # Allowed person (strong or uncertain): publishable — do not send to review.
        faces_out.append(entry)

    if per_face_blocked:
        reason = per_face_blocked[0]
        return AggregatedDecision(status="blocked", reason=reason, faces_out=faces_out)

    if per_face_review:
        reason = per_face_review[0]
        return AggregatedDecision(status="review", reason=reason, faces_out=faces_out)

    return AggregatedDecision(status="ok", reason="all_clear", faces_out=faces_out)
