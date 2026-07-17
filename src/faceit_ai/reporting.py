"""Aggregate GDPR decision counts and sample paths from SQLite (for CLI + post-run logs)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from faceit_ai.persistence.models import Asset, AssetDecision


@dataclass(frozen=True)
class DecisionSummary:
    counts_by_status: dict[str, int]
    total_assets_with_decision: int
    sample_paths_by_status: dict[str, list[str]]


def query_decision_summary(
    session: Session,
    *,
    samples_per_status: int = 5,
) -> DecisionSummary:
    count_rows = session.execute(
        select(AssetDecision.status, func.count(AssetDecision.id)).group_by(AssetDecision.status)
    ).all()
    counts = {str(row[0]): int(row[1]) for row in count_rows}
    total = sum(counts.values())

    samples: dict[str, list[str]] = {}
    for status in sorted(counts.keys()):
        stmt = (
            select(Asset.path)
            .join(AssetDecision, AssetDecision.asset_id == Asset.id)
            .where(AssetDecision.status == status)
            .order_by(Asset.path)
            .limit(max(0, samples_per_status))
        )
        paths = list(session.scalars(stmt).all())
        samples[status] = paths

    return DecisionSummary(
        counts_by_status=counts,
        total_assets_with_decision=total,
        sample_paths_by_status=samples,
    )


def summary_to_log_payload(summary: DecisionSummary) -> dict[str, Any]:
    """Structured blob suitable for logging or JSON."""
    return {
        "total_assets_with_decision": summary.total_assets_with_decision,
        "counts_by_status": dict(summary.counts_by_status),
        "sample_paths_by_status": dict(summary.sample_paths_by_status),
    }


def log_decision_database_summary(
    log: logging.Logger,
    summary: DecisionSummary,
    *,
    prefix: str = "database_decision_totals",
) -> None:
    """Counts only at INFO (full paths are noisy); use ``format_summary_text`` or JSON for samples."""
    log.info(
        "%s | total_assets_with_decision=%d (entire SQLite DB — all folders ever)",
    )
    for status in sorted(summary.counts_by_status.keys()):
        n = summary.counts_by_status[status]
        if n == 0:
            continue
        log.info("%s |   %s: %d", prefix, status, n)


def format_summary_text(summary: DecisionSummary) -> str:
    """Human-readable multi-line text for terminal output."""
    lines: list[str] = [
        f"Assets with a decision in DB: {summary.total_assets_with_decision}",
        "Counts by status:",
    ]
    for status in sorted(summary.counts_by_status.keys()):
        lines.append(f"  {status}: {summary.counts_by_status[status]}")
    lines.append("Sample paths (per status, alphabetical):")
    for status in sorted(summary.counts_by_status.keys()):
        if summary.counts_by_status[status] == 0:
            continue
        paths = summary.sample_paths_by_status.get(status, [])
        if not paths:
            lines.append(f"  [{status}] (no samples)")
            continue
        lines.append(f"  [{status}]")
        for p in paths:
            lines.append(f"    {p}")
    return "\n".join(lines)
