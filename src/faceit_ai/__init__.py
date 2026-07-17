"""Local GDPR-aware face recognition pipeline for photo moderation."""

from __future__ import annotations

from pathlib import Path

try:
    from importlib.metadata import PackageNotFoundError, version as _pkg_version
except ImportError:  # pragma: no cover
    PackageNotFoundError = Exception  # type: ignore[misc, assignment]
    _pkg_version = None  # type: ignore[assignment]

# Keep in sync with [project].version in pyproject.toml.
_FALLBACK_VERSION = "0.0.1"


def _version_from_pyproject() -> str | None:
    """Read version from pyproject.toml (source of truth for releases)."""
    try:
        import tomllib
    except ImportError:  # pragma: no cover
        return None
    # src/faceit_ai/__init__.py -> repo root pyproject.toml
    candidates = (
        Path(__file__).resolve().parents[2] / "pyproject.toml",
        Path(__file__).resolve().parents[1] / "pyproject.toml",
    )
    for pyproject in candidates:
        if not pyproject.is_file():
            continue
        try:
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
            v = str((data.get("project") or {}).get("version") or "").strip()
            if v:
                return v
        except Exception:
            continue
    return None


def _resolve_version() -> str:
    from_toml = _version_from_pyproject()
    if from_toml:
        return from_toml
    if _pkg_version is not None:
        try:
            return _pkg_version("faceit-ai")
        except PackageNotFoundError:
            pass
    return _FALLBACK_VERSION


__version__ = _resolve_version()
