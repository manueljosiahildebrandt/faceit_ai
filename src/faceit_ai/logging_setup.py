"""Structured audit logging: every decision and run is traceable."""

from __future__ import annotations

import json
import logging
import os
import sys
import warnings
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# `logging.LogRecord` extra key for colored phase lines on the console.
FACEIT_PHASE_KEY = "faceit_phase"
PHASE_START = "start"
PHASE_CHECK = "check"
PHASE_END = "end"

_ANSI = {
    "reset": "\033[0m",
    "bold": "\033[1m",
    "dim": "\033[2m",
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "blue": "\033[34m",
    "magenta": "\033[35m",
    "cyan": "\033[36m",
}


def stderr_supports_color() -> bool:
    return sys.stderr.isatty() and os.environ.get("NO_COLOR", "").strip() == ""


def format_elapsed(seconds: float) -> str:
    if seconds < 60.0:
        return f"{seconds:.1f}s"
    m = int(seconds // 60)
    s = seconds % 60.0
    return f"{m}m {s:.0f}s"


def log_run_phase(logger: logging.Logger, phase: str, msg: str, *args: object) -> None:
    """INFO log line styled on the console when `phase` is PHASE_*."""
    logger.info(msg, *args, extra={FACEIT_PHASE_KEY: phase})


def run_banner_lines(title: str, *, width: int = 58) -> tuple[str, str, str]:
    """Three ASCII lines: rule, centered title, rule (monospace-friendly)."""
    w = max(12, min(width, 120))
    rule = "-" * w
    mid = title.strip()
    if len(mid) > w - 2:
        mid = mid[: max(1, w - 3)] + "…"
    return (rule, mid.center(w), rule)


def log_run_banner(logger: logging.Logger, title: str, *, phase: str) -> None:
    """Emit a start/end style banner; use ``PHASE_START`` / ``PHASE_END`` for console colors."""
    for line in run_banner_lines(title):
        logger.info("%s", line, extra={FACEIT_PHASE_KEY: phase})


class ColoredConsoleFormatter(logging.Formatter):
    """TTY ANSI colors: phase highlights + level accents (audit/file handlers unchanged)."""

    def __init__(self, fmt: str, datefmt: str | None = None, *, use_color: bool = True) -> None:
        super().__init__(fmt, datefmt)
        self.use_color = use_color and stderr_supports_color()

    def format(self, record: logging.LogRecord) -> str:
        line = super().format(record)
        if not self.use_color:
            return line
        phase = getattr(record, FACEIT_PHASE_KEY, None)
        a = _ANSI
        if phase == PHASE_START:
            return f"{a['bold']}{a['cyan']}{line}{a['reset']}"
        if phase == PHASE_CHECK:
            return f"{a['magenta']}{line}{a['reset']}"
        if phase == PHASE_END:
            return f"{a['bold']}{a['green']}{line}{a['reset']}"
        if record.levelno >= logging.ERROR:
            return f"{a['bold']}{a['red']}{line}{a['reset']}"
        if record.levelno >= logging.WARNING:
            return f"{a['yellow']}{line}{a['reset']}"
        if record.levelno <= logging.DEBUG:
            return f"{a['dim']}{line}{a['reset']}"
        return line


class JsonAuditFormatter(logging.Formatter):
    """One JSON object per line for log aggregation and audits."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if hasattr(record, "audit"):
            payload["audit"] = getattr(record, "audit")
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def silence_noisy_libraries() -> None:
    """InsightFace pulls scikit-image/sklearn; suppress known deprecations and chatty loggers."""
    import os

    # Before onnxruntime loads (first inference). Reduces "Applied providers" / "find model" spam.
    os.environ.setdefault("ORT_LOG_SEVERITY_LEVEL", "3")

    warnings.filterwarnings(
        "ignore",
        message=r".*`estimate` is deprecated.*",
        category=FutureWarning,
    )
    warnings.filterwarnings(
        "ignore",
        message=r".*Please use `SimilarityTransform\.from_estimate`.*",
        category=FutureWarning,
    )
    for name in (
        "onnxruntime",
        "matplotlib",
        "matplotlib.font_manager",
        "PIL",
        "PIL.PngImagePlugin",
    ):
        logging.getLogger(name).setLevel(logging.WARNING)


def setup_logging(level: str, audit_log_path: Path) -> logging.Logger:
    silence_noisy_libraries()
    audit_log_path.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger("faceit_ai")
    root.handlers.clear()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    console_fmt = "%(asctime)s %(levelname)s %(name)s %(message)s"
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(ColoredConsoleFormatter(console_fmt, use_color=True))
    root.addHandler(sh)

    audit = logging.getLogger("faceit_ai.audit")
    audit.setLevel(logging.INFO)
    fh = logging.FileHandler(audit_log_path, encoding="utf-8")
    fh.setFormatter(JsonAuditFormatter())
    audit.addHandler(fh)
    # Avoid double propagation to root for audit lines
    audit.propagate = False
    return audit


def log_decision(
    audit: logging.Logger,
    *,
    asset_path: str,
    status: str,
    reason: str,
    usage: str,
    faces: list[dict[str, Any]],
    extra: dict[str, Any] | None = None,
) -> None:
    body = {
        "event": "asset_decision",
        "asset_path": asset_path,
        "status": status,
        "reason": reason,
        "usage": usage,
        "faces": faces,
    }
    if extra:
        body["extra"] = extra
    audit.info("decision", extra={"audit": body})


def log_metadata_sync(
    audit: logging.Logger | None,
    *,
    asset_path: str,
    status: str,
    writer: str,
    mode: str,
    success: bool,
    extra: dict[str, Any] | None = None,
) -> None:
    if audit is None:
        return
    body: dict[str, Any] = {
        "event": "metadata_sync",
        "asset_path": asset_path,
        "status": status,
        "writer": writer,
        "mode": mode,
        "success": success,
    }
    if extra:
        body["extra"] = extra
    audit.info("metadata_sync", extra={"audit": body})


def log_export_audit(
    audit: logging.Logger,
    *,
    src: str,
    dest: str,
    decision_status: str,
    action: str,
    extra: dict[str, Any] | None = None,
) -> None:
    body: dict[str, Any] = {
        "event": "asset_export",
        "src": src,
        "dest": dest,
        "decision_status": decision_status,
        "action": action,
    }
    if extra:
        body["extra"] = extra
    audit.info("export", extra={"audit": body})


def log_collect_audit(
    audit: logging.Logger,
    *,
    src: str,
    dest: str,
    person: str,
    action: str,
    extra: dict[str, Any] | None = None,
) -> None:
    body: dict[str, Any] = {
        "event": "asset_collect",
        "src": src,
        "dest": dest,
        "person": person,
        "action": action,
    }
    if extra:
        body["extra"] = extra
    audit.info("collect", extra={"audit": body})
