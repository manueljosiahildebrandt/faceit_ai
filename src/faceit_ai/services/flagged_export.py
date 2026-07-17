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
from faceit_ai.persistence.models import Asset, AssetDecision, CollectedPhoto
from faceit_ai.services.processing_runs import folder_claim_key


def _path_variants(path: Path | str) -> list[str]:
    """String forms to match ``Asset.path`` / ``CollectedPhoto.source_path`` after resolve."""
    text = str(path)
    out: list[str] = [text]
    try:
        out.append(str(Path(path).expanduser().resolve()))
    except OSError:
        pass
    # Dedupe while preserving order.
    return list(dict.fromkeys(out))


def repoint_asset_path_after_move(
    session: Session,
    *,
    old_paths: list[Path | str],
    new_path: Path | str,
    logger: logging.Logger | None = None,
) -> bool:
    """Point ``Asset.path`` (and matching ``CollectedPhoto.source_path``) at the post-move location.

    Called when flagged export ``action=move`` succeeds so later collect / gallery source
    links use the new path under ``flagged/…``, not the vacated original.
    """
    log = logger or logging.getLogger("faceit_ai")
    try:
        new_resolved = str(Path(new_path).expanduser().resolve())
    except OSError:
        new_resolved = str(new_path)

    old_variants = []
    for p in old_paths:
        old_variants.extend(_path_variants(p))
    old_variants = list(dict.fromkeys(old_variants))
    if not old_variants:
        return False

    asset: Asset | None = None
    for old in old_variants:
        asset = session.scalar(select(Asset).where(Asset.path == old))
        if asset is not None:
            break
    if asset is None:
        # Cross-OS / spelling mismatch: match by claim key + basename.
        old_keys = {folder_claim_key(o) for o in old_variants}
        name = Path(old_variants[0]).name
        if name:
            for row in session.execute(
                select(Asset).where(Asset.path.endswith(name))
            ).scalars():
                if folder_claim_key(row.path) in old_keys:
                    asset = row
                    break

    updated = False
    if asset is not None and asset.path != new_resolved:
        clash = session.scalar(select(Asset).where(Asset.path == new_resolved))
        if clash is not None and int(clash.id) != int(asset.id):
            log.warning(
                "export move: cannot repoint asset %s → %s (path already used by asset %s)",
                asset.path,
                new_resolved,
                clash.id,
            )
        else:
            asset.path = new_resolved
            updated = True

    for old in old_variants:
        for row in session.scalars(
            select(CollectedPhoto).where(CollectedPhoto.source_path == old)
        ).all():
            if row.source_path != new_resolved:
                row.source_path = new_resolved
                updated = True

    if updated:
        session.flush()
    return updated


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
                repoint_asset_path_after_move(
                    session,
                    old_paths=[src, source_file],
                    new_path=dest,
                    logger=log,
                )
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
                    repoint_asset_path_after_move(
                        session,
                        old_paths=[src_res, source_file],
                        new_path=dest,
                        logger=log,
                    )
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
            repoint_asset_path_after_move(
                session,
                old_paths=[src_res, source_file],
                new_path=dest,
                logger=log,
            )
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


def _find_asset_and_decision(
    session: Session,
    *,
    path: Path,
    root_res: Path,
) -> tuple[Asset | None, AssetDecision | None]:
    """Resolve ``Asset`` (+ decision) for a path under ``scan_root``."""
    candidates: list[Path] = [path]
    restore = _restore_path_outside_flagged(path, root_res)
    if restore is not None:
        candidates.append(restore)

    asset: Asset | None = None
    for candidate in candidates:
        for v in _path_variants(candidate):
            asset = session.scalar(select(Asset).where(Asset.path == v))
            if asset is not None:
                break
        if asset is not None:
            break

    if asset is None:
        keys = {folder_claim_key(str(c)) for c in candidates}
        name = path.name
        if name:
            for row in session.scalars(
                select(Asset).where(Asset.path.endswith(name))
            ).all():
                if folder_claim_key(row.path) in keys:
                    asset = row
                    break

    if asset is None:
        return None, None
    decision = session.scalar(
        select(AssetDecision).where(AssetDecision.asset_id == asset.id)
    )
    return asset, decision


def _flagged_tier_from_path(flagged_file: Path, flagged_base: Path) -> str | None:
    try:
        rel = flagged_file.resolve().relative_to(flagged_base)
    except ValueError:
        return None
    if rel.parts and rel.parts[0] in ("blocked", "review"):
        return str(rel.parts[0])
    return None


def _restore_path_outside_flagged(flagged_file: Path, root_res: Path) -> Path | None:
    """Map ``flagged/<tier>/…/file`` back to ``<scan_root>/…/file``."""
    try:
        rel = flagged_file.resolve().relative_to(root_res)
    except ValueError:
        return None
    parts = rel.parts
    if len(parts) >= 3 and parts[0] == "flagged" and parts[1] in ("blocked", "review"):
        return (root_res / Path(*parts[2:])).resolve()
    return None


def _is_stale_flagged_file(
    *,
    tier: str,
    decision: AssetDecision | None,
) -> bool:
    if decision is None:
        return True
    status = str(decision.status)
    if status == "ok":
        return True
    if status not in ("blocked", "review"):
        return True
    return status != tier


def prune_stale_flagged_exports(
    *,
    session: Session,
    scan_root: Path,
    action: Literal["copy", "move"],
    flagged_dirname: str = "flagged",
    audit: logging.Logger | None = None,
    logger: logging.Logger | None = None,
) -> tuple[int, int, list[str]]:
    """Remove flagged copies that no longer match DB decisions.

    Copy mode: delete stale files under ``flagged/{blocked,review}/``.
    Move mode: move restored files back outside ``flagged/`` and repoint ``Asset.path``.

    Returns ``(n_removed, n_restored, warnings)``.
    """
    log = logger or logging.getLogger("faceit_ai")
    root_res = scan_root.resolve()
    flagged_base = (root_res / flagged_dirname).resolve()
    if not flagged_base.is_dir():
        return 0, 0, []

    n_removed = 0
    n_restored = 0
    warnings: list[str] = []

    for tier in ("blocked", "review"):
        tier_root = flagged_base / tier
        if not tier_root.is_dir():
            continue
        for flagged_file in sorted(tier_root.rglob("*")):
            if not flagged_file.is_file():
                continue
            file_tier = _flagged_tier_from_path(flagged_file, flagged_base)
            if file_tier is None:
                continue

            asset, decision = _find_asset_and_decision(
                session, path=flagged_file, root_res=root_res
            )
            if not _is_stale_flagged_file(tier=file_tier, decision=decision):
                continue

            asset_path_matches_flagged = False
            if asset is not None:
                try:
                    asset_path_matches_flagged = (
                        Path(asset.path).resolve() == flagged_file.resolve()
                    )
                except OSError:
                    asset_path_matches_flagged = (
                        folder_claim_key(asset.path) == folder_claim_key(flagged_file)
                    )

            if action == "move" and asset_path_matches_flagged and asset is not None:
                restore = _restore_path_outside_flagged(flagged_file, root_res)
                if restore is None:
                    warnings.append(f"prune: cannot compute restore path for {flagged_file}")
                    continue
                if restore.exists() and restore.resolve() != flagged_file.resolve():
                    warnings.append(
                        f"prune: restore blocked, destination exists: {restore}"
                    )
                    continue
                try:
                    restore.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(flagged_file), str(restore))
                    repoint_asset_path_after_move(
                        session,
                        old_paths=[flagged_file, asset.path],
                        new_path=restore,
                        logger=log,
                    )
                    n_restored += 1
                    if audit is not None:
                        log_export_audit(
                            audit,
                            src=str(flagged_file),
                            dest=str(restore),
                            decision_status=str(decision.status if decision else "none"),
                            action="prune_restore",
                        )
                except OSError as e:
                    warnings.append(f"prune restore failed {flagged_file}: {e}")
                continue

            try:
                flagged_file.unlink()
                n_removed += 1
                if audit is not None:
                    log_export_audit(
                        audit,
                        src=str(flagged_file),
                        dest="",
                        decision_status=str(decision.status if decision else "none"),
                        action="prune_delete",
                        extra={"tier": file_tier},
                    )
            except OSError as e:
                warnings.append(f"prune delete failed {flagged_file}: {e}")

    if n_removed or n_restored:
        log.info(
            "prune_stale_flagged — removed=%d restored=%d root=%s action=%s",
            n_removed,
            n_restored,
            root_res,
            action,
        )
    return n_removed, n_restored, warnings
