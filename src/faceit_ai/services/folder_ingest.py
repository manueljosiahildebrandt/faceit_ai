"""Copy an entire source folder to an archive destination before analyze (e.g. SD → NAS)."""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

from faceit_ai.logging_setup import PHASE_CHECK, log_run_phase
from faceit_ai.vision.image_loader import path_is_under_flagged_tree


@dataclass(frozen=True)
class FolderIngestResult:
    dest_folder: Path
    n_copied: int
    n_skipped: int
    warnings: tuple[str, ...]


def resolve_ingest_destination(source: Path, dest_root: Path) -> Path:
    """Return ``dest_root / source.name`` with path-safety checks."""
    src = source.expanduser().resolve()
    root = dest_root.expanduser().resolve()
    if not src.is_dir():
        raise ValueError(f"Source is not a directory: {src}")
    if not root.is_dir():
        raise ValueError(f"Archive destination root is not a directory: {root}")
    try:
        root.relative_to(src)
        raise ValueError("Archive destination cannot be inside the source folder")
    except ValueError as e:
        if "cannot be inside" in str(e):
            raise
    try:
        src.relative_to(root)
        raise ValueError("Source folder cannot be inside the archive destination root")
    except ValueError as e:
        if "cannot be inside" in str(e):
            raise
    return root / src.name


def _is_under_flagged(path: Path, source_root: Path) -> bool:
    return path_is_under_flagged_tree(path, source_root)


def log_folder_ingest_audit(
    audit: logging.Logger,
    *,
    src: str,
    dest: str,
    action: str,
    extra: dict[str, object] | None = None,
) -> None:
    body: dict[str, object] = {
        "event": "folder_ingest",
        "src": src,
        "dest": dest,
        "action": action,
    }
    if extra:
        body["extra"] = extra
    audit.info("folder_ingest", extra={"audit": body})


def copy_folder_tree(
    source: Path,
    dest: Path,
    *,
    skip_flagged_subtree: bool = True,
    audit: logging.Logger | None = None,
    logger: logging.Logger | None = None,
) -> tuple[int, int, list[str]]:
    """Copy all files under ``source`` into ``dest``, preserving relative paths.

    When ``skip_flagged_subtree`` is true, skips files under ``source/flagged/``
    (typical for pre-analyze archive). Idempotent when dest exists with same size.
    Returns ``(n_copied, n_skipped_identical, warnings)``.
    """
    log = logger or logging.getLogger("faceit_ai")
    src = source.expanduser().resolve()
    dst = dest.expanduser().resolve()
    n_copied = 0
    n_skipped = 0
    warnings: list[str] = []

    if not src.is_dir():
        raise ValueError(f"Source is not a directory: {src}")

    dst.mkdir(parents=True, exist_ok=True)

    source_files = [p for p in sorted(src.rglob("*")) if p.is_file()]
    if not source_files:
        warnings.append(f"source folder has no files: {src}")
        return 0, 0, warnings

    for src_file in source_files:
        if skip_flagged_subtree and _is_under_flagged(src_file, src):
            continue
        rel = src_file.relative_to(src)
        dest_file = dst / rel
        dest_file.parent.mkdir(parents=True, exist_ok=True)

        if dest_file.is_file():
            try:
                if dest_file.stat().st_size == src_file.stat().st_size:
                    n_skipped += 1
                    if audit is not None:
                        log_folder_ingest_audit(
                            audit,
                            src=str(src_file),
                            dest=str(dest_file),
                            action="skip_identical",
                        )
                    continue
            except OSError:
                pass
            warnings.append(f"skip (destination exists, different size): {dest_file}")
            if audit is not None:
                log_folder_ingest_audit(
                    audit,
                    src=str(src_file),
                    dest=str(dest_file),
                    action="skip_conflict",
                    extra={"reason": "destination_exists"},
                )
            continue

        try:
            shutil.copy2(src_file, dest_file)
            n_copied += 1
            if audit is not None:
                log_folder_ingest_audit(
                    audit,
                    src=str(src_file),
                    dest=str(dest_file),
                    action="copy",
                )
        except OSError as e:
            warnings.append(f"copy failed {src_file} → {dest_file}: {e}")

    if warnings:
        for w in warnings[:30]:
            log.warning("folder_ingest: %s", w)
        if len(warnings) > 30:
            log.warning("folder_ingest: … %d more warnings omitted", len(warnings) - 30)

    log.info(
        "folder_ingest — done | copied=%d skipped_identical=%d dest=%s",
        n_copied,
        n_skipped,
        dst,
    )
    return n_copied, n_skipped, warnings


def run_folder_ingest(
    source: Path,
    dest_root: Path,
    *,
    skip_flagged_subtree: bool = True,
    phase_label: str = "Copying to archive",
    audit: logging.Logger | None = None,
    logger: logging.Logger | None = None,
) -> FolderIngestResult:
    """Copy ``source`` tree to ``dest_root / source.name`` and return the scan folder."""
    log = logger or logging.getLogger("faceit_ai")
    src = source.expanduser().resolve()
    dest = resolve_ingest_destination(src, dest_root)
    log_run_phase(
        log,
        PHASE_CHECK,
        "%s — %s → %s",
        phase_label,
        src,
        dest,
    )
    n_copied, n_skipped, warnings = copy_folder_tree(
        src, dest, skip_flagged_subtree=skip_flagged_subtree, audit=audit, logger=log
    )
    non_flagged = [
        p
        for p in src.rglob("*")
        if p.is_file() and (not skip_flagged_subtree or not _is_under_flagged(p, src))
    ]
    if non_flagged and n_copied == 0 and n_skipped == 0 and warnings:
        raise RuntimeError(
            f"Archive copy failed for {src} → {dest}: no files copied or skipped"
        )
    return FolderIngestResult(
        dest_folder=dest,
        n_copied=n_copied,
        n_skipped=n_skipped,
        warnings=tuple(warnings),
    )
