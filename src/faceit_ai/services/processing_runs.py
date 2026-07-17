"""Folder-analysis claims so multiple PCs can share one database safely.

A "claim" is a row in ``processing_run`` (see models). Because every PC talks to the
same database, the database itself arbitrates who owns a folder - no lock files on the NAS.
"""

from __future__ import annotations

import logging
import socket
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from faceit_ai.persistence.models import ProcessingRun
from faceit_ai.persistence.session import session_scope

# A run whose heartbeat is older than this is treated as crashed and reclaimable.
STALE_AFTER = timedelta(hours=6)

log = logging.getLogger("faceit_ai")


def this_host() -> str:
    try:
        return socket.gethostname() or "unknown-host"
    except OSError:
        return "unknown-host"


def _normalize(folder: Path | str) -> str:
    return str(Path(folder).expanduser().resolve())


@dataclass(frozen=True)
class ClaimResult:
    run_id: int | None
    claimed: bool
    holder_host: str | None = None
    holder_started_at: datetime | None = None


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _reap_stale(session: Any, folder_path: str) -> None:
    cutoff = datetime.now(UTC) - STALE_AFTER
    rows = session.scalars(
        select(ProcessingRun).where(
            ProcessingRun.folder_path == folder_path,
            ProcessingRun.finished_at.is_(None),
        )
    ).all()
    for row in rows:
        ref = _aware(row.updated_at) or _aware(row.started_at)
        if ref is not None and ref < cutoff:
            row.status = "stale"
            row.finished_at = datetime.now(UTC)
            row.message = "auto-reaped (no heartbeat)"


def claim_folder(
    session_factory: sessionmaker[Any],
    folder: Path | str,
    *,
    host: str | None = None,
) -> ClaimResult:
    """Try to claim a folder for analysis. Returns ClaimResult(claimed=False) if busy."""
    folder_path = _normalize(folder)
    host = host or this_host()

    try:
        with session_scope(session_factory) as session:
            _reap_stale(session, folder_path)
            session.flush()

            existing = session.scalar(
                select(ProcessingRun).where(
                    ProcessingRun.folder_path == folder_path,
                    ProcessingRun.finished_at.is_(None),
                )
            )
            if existing is not None:
                return ClaimResult(
                    run_id=None,
                    claimed=False,
                    holder_host=existing.host,
                    holder_started_at=_aware(existing.started_at),
                )

            run = ProcessingRun(
                folder_path=folder_path,
                host=host,
                status="running",
            )
            session.add(run)
            session.flush()
            return ClaimResult(run_id=int(run.id), claimed=True)
    except IntegrityError:
        # Another PC won the race on the partial unique index.
        with session_scope(session_factory) as session:
            existing = session.scalar(
                select(ProcessingRun).where(
                    ProcessingRun.folder_path == folder_path,
                    ProcessingRun.finished_at.is_(None),
                )
            )
            return ClaimResult(
                run_id=None,
                claimed=False,
                holder_host=existing.host if existing else None,
                holder_started_at=_aware(existing.started_at) if existing else None,
            )


def heartbeat(session_factory: sessionmaker[Any], run_id: int | None) -> None:
    if run_id is None:
        return
    with session_scope(session_factory) as session:
        run = session.get(ProcessingRun, run_id)
        if run is not None and run.finished_at is None:
            run.updated_at = datetime.now(UTC)


def finish_run(
    session_factory: sessionmaker[Any],
    run_id: int | None,
    *,
    status: str = "done",
    message: str | None = None,
) -> None:
    if run_id is None:
        return
    with session_scope(session_factory) as session:
        run = session.get(ProcessingRun, run_id)
        if run is not None and run.finished_at is None:
            run.status = status
            run.message = message
            run.finished_at = datetime.now(UTC)


def release_folder_claims(
    session_factory: sessionmaker[Any],
    folder: Path | str,
    *,
    host: str | None = None,
    status: str = "cancelled",
    message: str | None = None,
) -> int:
    """Finish open claims for ``folder``. If ``host`` is set, only that host's rows.

    Used when a local analyze process is killed (SIGTERM/SIGKILL) and cannot run its
    own ``finish_run`` cleanup — otherwise the folder stays locked for hours.
    """
    folder_path = _normalize(folder)
    n = 0
    with session_scope(session_factory) as session:
        q = select(ProcessingRun).where(
            ProcessingRun.folder_path == folder_path,
            ProcessingRun.finished_at.is_(None),
        )
        if host is not None:
            q = q.where(ProcessingRun.host == host)
        rows = list(session.scalars(q).all())
        now = datetime.now(UTC)
        for row in rows:
            row.status = status
            row.message = message
            row.finished_at = now
            n += 1
    return n


def list_active_runs(session_factory: sessionmaker[Any]) -> list[dict[str, Any]]:
    """Active runs across all machines (for the UI 'who is running what' view)."""
    cutoff = datetime.now(UTC) - STALE_AFTER
    out: list[dict[str, Any]] = []
    with session_scope(session_factory) as session:
        rows = session.scalars(
            select(ProcessingRun)
            .where(ProcessingRun.finished_at.is_(None))
            .order_by(ProcessingRun.started_at)
        ).all()
        for row in rows:
            ref = _aware(row.updated_at) or _aware(row.started_at)
            if ref is not None and ref < cutoff:
                continue  # stale; ignore in the live view
            out.append(
                {
                    "id": int(row.id),
                    "folder": row.folder_path,
                    "folder_name": Path(row.folder_path).name or row.folder_path,
                    "host": row.host,
                    "status": row.status,
                    "started_at": ref.isoformat() if ref else None,
                }
            )
    return out
