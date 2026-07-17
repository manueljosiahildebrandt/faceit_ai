"""CLI entry points: thin wrappers over services."""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path
from typing import Literal, cast

import click

from faceit_ai.integration.metadata_port import build_metadata_sync
from faceit_ai.logging_setup import (
    PHASE_CHECK,
    PHASE_END,
    PHASE_START,
    format_elapsed,
    log_run_phase,
    setup_logging,
)
from faceit_ai.persistence.session import (
    create_engine_and_session_factory,
    init_db,
    session_scope,
)
from faceit_ai.reporting import format_summary_text, query_decision_summary
from faceit_ai.services.analyze_photos import run_analyze, was_analyze_stopped_early
from faceit_ai.services.folder_ingest import resolve_ingest_destination
from faceit_ai.services.processing_runs import claim_folder, finish_run
from faceit_ai.services.register_person import run_register
from faceit_ai.services.redecide_and_sync_person import run_redecide_and_sync_person
from faceit_ai.services.set_consent import run_set_consent
from faceit_ai.services.sync_metadata import run_sync_metadata
from faceit_ai.settings import IngestOrder, load_settings
from faceit_ai.vision.insightface_backend import InsightFaceBackend


@click.command("analyze_photos")
@click.argument("folder", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option(
    "--usage",
    "usage",
    required=True,
    type=click.Choice(["social", "web", "internal", "print"]),
    help="Target publishing usage; checked against consent flags.",
)
@click.option("--force", is_flag=True, help="Reprocess even if SHA-256 already has a decision.")
@click.option("--debug", is_flag=True, help="Verbose logging.")
@click.option(
    "--json-out",
    "json_out",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Directory for per-image JSON results.",
)
@click.option(
    "--quiet",
    is_flag=True,
    help="No progress bar; keep console output minimal.",
)
@click.option(
    "--export-flagged",
    type=click.Choice(["off", "copy", "move", "from-config"], case_sensitive=False),
    default="copy",
    show_default=True,
    help="Export blocked/review files under <folder>/flagged/{blocked,review}/. "
    "Default copy. Use off|move, or from-config for export.flagged in YAML.",
)
@click.option(
    "--sync-metadata/--no-sync-metadata",
    default=False,
    show_default=True,
    help="After analyze completes, run metadata sync for selected statuses as batch.",
)
@click.option(
    "--flagged-status",
    multiple=True,
    type=click.Choice(["blocked", "review"]),
    default=["blocked", "review"],
    help="Which decision statuses to export (can pass twice). Ignored if export-flagged=off.",
)
@click.option(
    "--collect-to",
    "collect_to",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Copy strong-matched photos into <PATH>/<person>/ for later manual re-registration. "
    "Falls back to collect.people_root in YAML when omitted.",
)
@click.option(
    "--collect-crop/--no-collect-crop",
    "collect_crop",
    default=None,
    help="Crop face portraits when collecting to people folders. Default from collect.crop_portrait in YAML.",
)
@click.option(
    "--ingest-to",
    "ingest_to",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Copy the entire source folder tree here before analyze (copy-only; source unchanged). "
    "Required when archive is enabled from the web UI.",
)
@click.option(
    "--no-ingest",
    is_flag=True,
    help="Skip archive copy even when ingest.enabled is set in YAML.",
)
@click.option(
    "--ingest-order",
    "ingest_order",
    type=click.Choice(["copy-then-analyze", "analyze-then-copy"], case_sensitive=False),
    default=None,
    help="Archive workflow order. Default from ingest.order in YAML (copy-then-analyze).",
)
def analyze_photos_cli(
    folder: Path,
    usage: str,
    force: bool,
    debug: bool,
    json_out: Path | None,
    quiet: bool,
    export_flagged: str,
    sync_metadata: bool,
    flagged_status: tuple[str, ...],
    collect_to: Path | None,
    collect_crop: bool | None,
    ingest_to: Path | None,
    no_ingest: bool,
    ingest_order: str | None,
) -> None:
    settings = load_settings()
    level = "DEBUG" if debug else settings.logging.level
    audit = setup_logging(level, settings.logging.audit_log_path)
    _, session_factory = create_engine_and_session_factory(settings.database_url)
    backend = InsightFaceBackend(settings.insightface_root, settings.pipeline.insightface)
    eff_export = export_flagged.lower()
    if eff_export == "from-config":
        eff_export = settings.export.flagged
    collect_people_root = collect_to if collect_to is not None else settings.collect.people_root

    ingest_dest_root: Path | None = None
    if not no_ingest and ingest_to is not None:
        ingest_dest_root = ingest_to

    eff_ingest_order: IngestOrder = settings.ingest.order
    if ingest_order is not None:
        eff_ingest_order = (
            "analyze_then_copy"
            if ingest_order.lower() == "analyze-then-copy"
            else "copy_then_analyze"
        )

    metadata_folder = folder
    claim_folder_path = folder
    if ingest_dest_root is not None and eff_ingest_order == "copy_then_analyze":
        archive_scan = resolve_ingest_destination(folder, ingest_dest_root)
        metadata_folder = archive_scan
        claim_folder_path = archive_scan

    # Claim this folder so a second PC on the shared DB won't process it in parallel.
    claim = claim_folder(session_factory, claim_folder_path)
    if not claim.claimed:
        click.echo(
            f"Folder is already being analyzed by {claim.holder_host!r} "
            f"(since {claim.holder_started_at}). Skipping to avoid duplicate work.",
            err=True,
        )
        sys.exit(3)

    run_status = "done"
    try:
        # Phase 1: analysis only (no per-file metadata writes in hot loop).
        run_analyze(
            folder=folder,
            usage=usage,
            settings=settings,
            session_factory=session_factory,
            backend=backend,
            audit=audit,
            metadata=None,
            force=force,
            json_out_dir=json_out,
            show_progress=not quiet,
            export_flagged=cast(Literal["off", "copy", "move"], eff_export),
            flagged_statuses=tuple(flagged_status) if flagged_status else ("blocked", "review"),
            collect_people_root=collect_people_root,
            collect_crop=collect_crop,
            ingest_dest_root=ingest_dest_root,
            ingest_order=eff_ingest_order,
            run_id=claim.run_id,
        )
        if was_analyze_stopped_early():
            run_status = "cancelled"
            logging.getLogger("faceit_ai").info(
                "Analysis stopped early; flagged/collect checkout completed for processed photos."
            )
        # Phase 2: optional metadata sync as a separate batch step (also after early stop).
        if sync_metadata:
            metadata_sync = build_metadata_sync(
                settings,
                log=logging.getLogger("faceit_ai.metadata"),
                audit=audit,
            )
            summary = run_sync_metadata(
                folder=metadata_folder,
                settings=settings,
                session_factory=session_factory,
                metadata=metadata_sync,
                audit=audit,
                show_progress=not quiet,
                statuses=tuple(str(s).lower() for s in flagged_status)
                if flagged_status
                else ("blocked", "review"),
            )
            click.echo(
                f"post-analyze sync_metadata: synced={summary['synced']}, "
                f"no_db_match={summary['skipped_no_db_match']}, "
                f"skipped_status={summary['skipped_status']}, "
                f"errors={summary['errors']}, scanned={summary['scanned']}"
            )
    except Exception:
        run_status = "failed"
        logging.getLogger("faceit_ai").exception("analyze_photos failed")
        sys.exit(1)
    finally:
        finish_run(session_factory, claim.run_id, status=run_status)


@click.command("set_person_consent")
@click.argument("name")
@click.option(
    "--revoke",
    is_flag=True,
    help="Set consent_given=false → matched faces block (GDPR no consent).",
)
@click.option(
    "--grant",
    is_flag=True,
    help="Set consent_given=true → matched faces allowed if usage flags permit.",
)
def set_person_consent_cli(name: str, revoke: bool, grant: bool) -> None:
    """Change consent for an existing person (does not re-scan photos). Re-run analyze_photos --force after."""
    if revoke == grant:
        raise click.UsageError("Specify exactly one of --revoke or --grant.")
    settings = load_settings()
    setup_logging(settings.logging.level, settings.logging.audit_log_path)
    _, session_factory = create_engine_and_session_factory(settings.database_url)
    try:
        run_set_consent(
            person_name=name,
            consent_given=grant,
            session_factory=session_factory,
        )
        click.echo(
            f"Updated {name!r}: consent_given={grant}. "
            "Re-run: analyze_photos <folder> --usage … --force"
        )
    except ValueError as e:
        click.echo(str(e), err=True)
        sys.exit(1)


@click.command("consent_and_sync_person")
@click.argument("name")
@click.option(
    "--allowed",
    type=click.Choice(["allowed", "blocked"], case_sensitive=False),
    required=True,
    help="Set GDPR consent for this person (blocked → consent_given=false).",
)
def consent_and_sync_person_cli(name: str, allowed: str) -> None:
    """Update person consent and immediately re-decision + metadata sync.

    This is operator-friendly: it avoids requiring `analyze_photos --force` just
    to refresh Lightroom labels after changing consent.
    """
    settings = load_settings()
    audit = setup_logging(settings.logging.level, settings.logging.audit_log_path)
    _, session_factory = create_engine_and_session_factory(settings.database_url)

    metadata_sync = build_metadata_sync(
        settings,
        log=logging.getLogger("faceit_ai.metadata"),
        audit=audit,
    )

    consent_allowed = allowed.lower() == "allowed"
    try:
        res = run_redecide_and_sync_person(
            person_name=name,
            consent_allowed=consent_allowed,
            settings=settings,
            session_factory=session_factory,
            metadata=metadata_sync,
            audit=audit,
        )
        click.echo(
            f"Updated consent for {name!r} → {allowed.lower()}. "
            f"Affected assets={res.affected_assets}, metadata_applied={res.metadata_applied}."
        )
    except ValueError as e:
        click.echo(str(e), err=True)
        sys.exit(1)
    except Exception:
        logging.getLogger("faceit_ai").exception("consent_and_sync_person failed")
        sys.exit(1)


@click.command("register_person")
@click.argument("folder", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--name", required=True, help="Display name for the consent database.")
@click.option(
    "--no-consent",
    "no_consent",
    is_flag=True,
    default=False,
    help="If set, consent_given is stored as false (images with this person should block).",
)
@click.option("--quiet", is_flag=True, help="No progress bar.")
def register_person_cli(folder: Path, name: str, no_consent: bool, quiet: bool) -> None:
    settings = load_settings()
    audit = setup_logging(settings.logging.level, settings.logging.audit_log_path)
    _ = audit
    _, session_factory = create_engine_and_session_factory(settings.database_url)
    backend = InsightFaceBackend(settings.insightface_root, settings.pipeline.insightface)

    try:
        n = run_register(
            photo_folder=folder,
            name=name,
            settings=settings,
            session_factory=session_factory,
            backend=backend,
            consent_given=not no_consent,
            show_progress=not quiet,
        )
        click.echo(f"Added {n} embedding(s) for {name!r}.")
    except Exception as e:
        logging.getLogger("faceit_ai").exception("register_person failed")
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@click.command("sync_metadata")
@click.argument("folder", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--debug", is_flag=True, help="Verbose logging.")
@click.option("--quiet", is_flag=True, help="No progress bar.")
@click.option(
    "--statuses",
    multiple=True,
    type=click.Choice(["blocked", "review", "ok"]),
    default=("blocked", "review"),
    show_default=True,
    help="Decision statuses to write metadata for.",
)
def sync_metadata_cli(folder: Path, debug: bool, quiet: bool, statuses: tuple[str, ...]) -> None:
    """
    Re-apply XMP/metadata from SQLite decisions for files under FOLDER (no face re-analysis).

    Use after changing ``lightroom.xmp_label_values`` or metadata settings, or when
    ``analyze_photos`` skipped cached assets without writing metadata.
    """
    settings = load_settings()
    level = "DEBUG" if debug else settings.logging.level
    audit = setup_logging(level, settings.logging.audit_log_path)
    _, session_factory = create_engine_and_session_factory(settings.database_url)
    metadata_sync = build_metadata_sync(
        settings,
        log=logging.getLogger("faceit_ai.metadata"),
        audit=audit,
    )
    if not settings.metadata.enabled:
        click.echo(
            "metadata.enabled is false; enable metadata in config or nothing will be written.",
            err=True,
        )
    try:
        summary = run_sync_metadata(
            folder=folder,
            settings=settings,
            session_factory=session_factory,
            metadata=metadata_sync,
            audit=audit,
            show_progress=not quiet,
            statuses=tuple(str(s).lower() for s in statuses),
        )
        click.echo(
            f"sync_metadata: synced={summary['synced']}, "
            f"no_db_match={summary['skipped_no_db_match']}, "
            f"skipped_status={summary['skipped_status']}, "
            f"errors={summary['errors']}, scanned={summary['scanned']}"
        )
    except Exception:
        logging.getLogger("faceit_ai").exception("sync_metadata failed")
        sys.exit(1)


@click.command("report_decisions")
@click.option(
    "--samples",
    default=5,
    type=int,
    show_default=True,
    help="Max example paths to show per status (alphabetical).",
)
def report_decisions_cli(samples: int) -> None:
    """Print decision counts and sample file paths from the SQLite DB."""
    settings = load_settings()
    setup_logging(settings.logging.level, settings.logging.audit_log_path)
    log = logging.getLogger("faceit_ai")
    t0 = time.perf_counter()
    log_run_phase(log, PHASE_START, "report_decisions — querying SQLite…")
    _, session_factory = create_engine_and_session_factory(settings.database_url)
    with session_scope(session_factory) as session:
        summary = query_decision_summary(session, samples_per_status=max(0, samples))
    log_run_phase(log, PHASE_CHECK, "report_decisions — printing summary to stdout")
    click.echo(format_summary_text(summary))
    elapsed = time.perf_counter() - t0
    log_run_phase(
        log, PHASE_END, "report_decisions — finished in %s", format_elapsed(elapsed)
    )


def _mask_db_url(url: str) -> str:
    """Hide any password in a SQLAlchemy URL before echoing it."""
    if "@" in url and "://" in url:
        scheme, rest = url.split("://", 1)
        if "@" in rest:
            creds, host = rest.split("@", 1)
            user = creds.split(":", 1)[0]
            return f"{scheme}://{user}:***@{host}"
    return url


@click.command("init_db")
def init_db_cli() -> None:
    """Create database tables for the configured database URL (safe to re-run).

    Run this once per new database (e.g. after pointing database.url at Postgres).
    """
    settings = load_settings()
    setup_logging(settings.logging.level, settings.logging.audit_log_path)
    init_db(settings.database_url)
    click.echo(f"Initialized database schema at {_mask_db_url(settings.database_url)}")


@click.command("migrate_sqlite_to_db")
@click.option(
    "--source",
    "source",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="Path to the existing SQLite file (e.g. data/consent.db).",
)
@click.option(
    "--target-url",
    "target_url",
    default=None,
    help="Target SQLAlchemy URL. Defaults to the configured database.url.",
)
def migrate_sqlite_to_db_cli(source: Path, target_url: str | None) -> None:
    """One-time copy of a local SQLite DB into the shared database (e.g. Postgres)."""
    from faceit_ai.services.migrate_db import migrate_sqlite_to_url

    settings = load_settings()
    setup_logging(settings.logging.level, settings.logging.audit_log_path)
    dest = target_url or settings.database_url
    if dest.startswith("sqlite:") and Path(dest.replace("sqlite:///", "")) == source.resolve():
        click.echo("Target equals source; nothing to do.", err=True)
        sys.exit(1)
    try:
        counts = migrate_sqlite_to_url(source, dest)
    except RuntimeError as e:
        click.echo(str(e), err=True)
        sys.exit(1)
    except Exception:
        logging.getLogger("faceit_ai").exception("migrate_sqlite_to_db failed")
        sys.exit(1)
    summary = ", ".join(f"{k}={v}" for k, v in counts.items())
    click.echo(f"Migrated into {_mask_db_url(dest)}: {summary}")


def main() -> None:
    # Reserved for `python -m faceit_ai` if you add a group later.
    click.echo(
        "Use console_scripts: analyze_photos / sync_metadata / register_person / "
        "set_person_consent / report_decisions",
        err=True,
    )


if __name__ == "__main__":
    main()
