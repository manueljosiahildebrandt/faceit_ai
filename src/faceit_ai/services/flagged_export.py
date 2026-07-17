"""Copy or move assets into ``<scan_root>/flagged/<blocked|review>/`` mirroring relative paths."""

from __future__ import annotations

import logging
import shutil
from collections.abc import Collection
from pathlib import Path
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from faceit_ai.logging_setup import PHASE_CHECK, log_export_audit, log_run_phase
from faceit_ai.persistence.models import Asset, AssetDecision


def _already_in_tiered_export_tree(src: Path, flagged_base: Path) -> bool:
    """True if ``src`` is already under ``flagged/blocked`` or ``flagged/review``."""
    try:
        r = src.relative_to(flagged_base)
    except ValueError:
        return False
    return len(r.parts) >= 1 and r.parts[0] in ("blocked", "review")


def _resolve_source_file(db_path: Path, root_res: Path) -> Path | None:
    """
    Prefer the DB path. If that file is gone, try the common layout where the DB still
    stores ``<root>/flagged/<name>`` but the image was moved back to ``<root>/<name>``.
    """
    if db_path.is_file():
        return db_path
    try:
        rel = db_path.relative_to(root_res)
    except ValueError:
        return None
    parts = rel.parts
    if len(parts) >= 2 and parts[0] == "flagged" and parts[1] not in ("blocked", "review"):
        alt = root_res.joinpath(*parts[1:])
        if alt.is_file():
            return alt
    return None


def _rel_for_tiered_destination(rel_under_root: Path) -> Path:
    """
    Legacy exports used ``<root>/flagged/<file>`` (flat). Tiered layout uses
    ``<root>/flagged/<blocked|review>/<file>``. Strip a single leading ``flagged/``
    segment when it is not already ``flagged/blocked`` or ``flagged/review`` so we do
    not build ``.../review/flagged/DSC…``.
    """
    parts = rel_under_root.parts
    if (
        len(parts) >= 2
        and parts[0] == "flagged"
        and parts[1] not in ("blocked", "review")
    ):
        return Path(*parts[1:])
    return rel_under_root


def list_resolved_paths_with_status(
    session: Session,
    scan_root: Path,
    statuses: Collection[str],
) -> list[tuple[Path, str]]:
    """``(resolved_path, decision_status)`` under ``scan_root`` for assets in ``statuses``."""
    root_res = scan_root.resolve()
    st_set = frozenset(statuses)
    if not st_set:
        return []
    stmt = (
        select(Asset.path, AssetDecision.status)
        .join(AssetDecision, AssetDecision.asset_id == Asset.id)
        .where(AssetDecision.status.in_(st_set))
    )
    out: list[tuple[Path, str]] = []
    for path_str, status in session.execute(stmt):
        p = Path(path_str).expanduser()
        try:
            pr = p.resolve()
        except OSError:
            continue
        try:
            pr.relative_to(root_res)
        except ValueError:
            continue
        out.append((pr, str(status)))
    out.sort(key=lambda t: (t[1], str(t[0])))
    return out


def export_flagged_under_folder(
    *,
    session: Session,
    scan_root: Path,
    statuses: Collection[str],
    action: Literal["copy", "move"],
    flagged_dirname: str = "flagged",
    audit: logging.Logger | None = None,
    logger: logging.Logger | None = None,
) -> tuple[int, int, list[str]]:
    """
    For each matching asset under ``scan_root``, copy/move to
    ``scan_root / flagged_dirname / <blocked|review> / <relative_path>``.

    Skips sources already under ``flagged/blocked`` or ``flagged/review`` (tiered tree).
    Flat ``flagged/<file>`` from older exports is migrated into the correct subfolder.
    If the destination file already exists with the same size as the source, skips (idempotent).

    Returns ``(n_ok, n_missing, warnings)``.
    """
    log = logger or logging.getLogger("faceit_ai")
    root_res = scan_root.resolve()
    flagged_base = (root_res / flagged_dirname).resolve()

    jobs = list_resolved_paths_with_status(session, scan_root, statuses)
    log_run_phase(
        log,
        PHASE_CHECK,
        "export_flagged — %d path(s) with status in %s under %s; action=%s → %s/{blocked,review}/",
        len(jobs),
        sorted(frozenset(statuses)),
        root_res,
        action,
        flagged_base,
    )

    n_ok = 0
    n_missing = 0
    warnings: list[str] = []

    for src, decision_status in jobs:
        if decision_status not in ("blocked", "review"):
            warnings.append(f"skip (unsupported status {decision_status!r}): {src}")
            continue

        try:
            rel = src.relative_to(root_res)
        except ValueError:
            warnings.append(f"skip (not under root): {src}")
            continue

        if _already_in_tiered_export_tree(src, flagged_base):
            continue

        source_file = _resolve_source_file(src, root_res)
        if source_file is None:
            n_missing += 1
            warnings.append(f"missing on disk: {src}")
            continue

        rel_dest = _rel_for_tiered_destination(rel)
        dest_root = (flagged_base / decision_status).resolve()
        dest = dest_root / rel_dest
        dest.parent.mkdir(parents=True, exist_ok=True)

        if dest.is_file():
            try:
                if dest.stat().st_size == source_file.stat().st_size:
                    if audit is not None:
                        log_export_audit(
                            audit,
                            src=str(source_file),
                            dest=str(dest),
                            decision_status=decision_status,
                            action="skip_identical",
                            extra={"reason": "destination_exists_same_size"},
                        )
                    continue
            except OSError:
                pass
            warnings.append(f"skip (destination exists, different size): {dest}")
            if audit is not None:
                log_export_audit(
                    audit,
                    src=str(source_file),
                    dest=str(dest),
                    decision_status=decision_status,
                    action="skip_conflict",
                    extra={"reason": "destination_exists"},
                )
            continue

        try:
            if action == "copy":
                shutil.copy2(source_file, dest)
            else:
                shutil.move(str(source_file), str(dest))
            n_ok += 1
            if audit is not None:
                log_export_audit(
                    audit,
                    src=str(source_file),
                    dest=str(dest),
                    decision_status=decision_status,
                    action=action,
                    extra={"db_path": str(src)} if source_file != src else None,
                )
        except OSError as e:
            warnings.append(f"{action} failed {source_file} → {dest}: {e}")

    if warnings:
        for w in warnings[:50]:
            log.warning("%s", w)
        if len(warnings) > 50:
            log.warning("… %d more export warnings omitted", len(warnings) - 50)

    log.info(
        "export_flagged — done | ok=%d missing=%d action=%s dest=%s",
        n_ok,
        n_missing,
        action,
        flagged_base,
    )
    return n_ok, n_missing, warnings


def export_single_flagged_asset(
    *,
    session: Session,
    scan_root: Path,
    source_path: Path,
    decision_status: Literal["blocked", "review"],
    action: Literal["copy", "move"],
    flagged_dirname: str = "flagged",
    audit: logging.Logger | None = None,
    logger: logging.Logger | None = None,
) -> tuple[int, int, list[str]]:
    """Copy or move one asset into ``scan_root/flagged/<status>/…``.

    Returns ``(n_ok, n_missing, warnings)`` — ``n_ok`` is 0 or 1.
    """
    log = logger or logging.getLogger("faceit_ai")
    root_res = scan_root.resolve()
    flagged_base = (root_res / flagged_dirname).resolve()
    src = source_path.expanduser()
    try:
        src_res = src.resolve()
    except OSError:
        return 0, 1, [f"missing on disk: {source_path}"]

    try:
        rel = src_res.relative_to(root_res)
    except ValueError:
        return 0, 0, [f"skip (not under root): {src_res}"]

    if _already_in_tiered_export_tree(src_res, flagged_base):
        if decision_status == "blocked":
            try:
                rel_in = src_res.relative_to(flagged_base / "review")
                dest_root = (flagged_base / "blocked").resolve()
                dest = dest_root / _rel_for_tiered_destination(rel_in)
                dest.parent.mkdir(parents=True, exist_ok=True)
                source_file = src_res
                if dest.is_file():
                    try:
                        if dest.stat().st_size == source_file.stat().st_size:
                            return 0, 0, []
                    except OSError:
                        pass
                if action == "copy":
                    shutil.copy2(source_file, dest)
                else:
                    shutil.move(str(source_file), str(dest))
                if audit is not None:
                    log_export_audit(
                        audit,
                        src=str(source_file),
                        dest=str(dest),
                        decision_status=decision_status,
                        action=action,
                        extra={"reason": "review_to_blocked"},
                    )
                return 1, 0, []
            except ValueError:
                return 0, 0, []
        return 0, 0, []

    source_file = _resolve_source_file(src_res, root_res)
    if source_file is None:
        return 0, 1, [f"missing on disk: {src_res}"]

    rel_dest = _rel_for_tiered_destination(rel)
    dest = (flagged_base / decision_status).resolve() / rel_dest
    dest.parent.mkdir(parents=True, exist_ok=True)

    if dest.is_file():
        try:
            if dest.stat().st_size == source_file.stat().st_size:
                return 0, 0, []
        except OSError:
            pass
        return 0, 0, [f"skip (destination exists): {dest}"]

    try:
        if action == "copy":
            shutil.copy2(source_file, dest)
        else:
            shutil.move(str(source_file), str(dest))
        if audit is not None:
            log_export_audit(
                audit,
                src=str(source_file),
                dest=str(dest),
                decision_status=decision_status,
                action=action,
            )
        return 1, 0, []
    except OSError as e:
        log.warning("export_single failed %s → %s: %s", source_file, dest, e)
        return 0, 0, [f"{action} failed: {e}"]


# Backwards-compatible name for tests importing list shape
def list_resolved_paths_under_root(
    session: Session,
    scan_root: Path,
    statuses: Collection[str],
) -> list[Path]:
    return [p for p, _ in list_resolved_paths_with_status(session, scan_root, statuses)]
