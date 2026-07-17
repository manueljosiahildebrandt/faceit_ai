"""Lightroom / XMP metadata sync port (no-op, XMP sidecar, future JPEG embed)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from faceit_ai.settings import LightroomSettings, MetadataIntegrationSettings, Settings


@dataclass(frozen=True)
class MetadataWriteRequest:
    file_path: str
    status: str
    reason: str
    usage: str
    face_count: int | None = None
    faces_identified: int | None = None
    match_confidence_max: float | None = None


class MetadataSyncPort(Protocol):
    def apply(self, req: MetadataWriteRequest) -> None: ...


class NoOpMetadataSync:
    def apply(self, req: MetadataWriteRequest) -> None:
        return None


def _lightroom_label_token(yaml_value: str) -> str | None:
    t = yaml_value.strip()
    if not t or t.lower() == "none":
        return None
    return t.title()


class XmpSidecarMetadataSync:
    """Writes ``.xmp`` next to images; failures are logged, not raised."""

    def __init__(
        self,
        meta: MetadataIntegrationSettings,
        lr: LightroomSettings,
        log: logging.Logger | None = None,
    ) -> None:
        self._meta = meta
        self._lr = lr
        self._log = log or logging.getLogger("faceit_ai.metadata")

    def apply(self, req: MetadataWriteRequest) -> None:
        from faceit_ai.integration.xmp_sidecar import write_sidecar

        if self._meta.mode != "xmp_sidecar":
            self._log.warning("metadata mode %r not implemented; skipping", self._meta.mode)
            return

        color: str | None = None
        if self._lr.enable and self._meta.write_color_label:
            st = req.status.lower()
            if st in self._lr.xmp_label_values:
                raw = self._lr.xmp_label_values[st]
                xs = str(raw).strip()
                if xs == "" or xs.lower() in ("none", "null", "~"):
                    color = None
                else:
                    color = xs
            else:
                spec = self._meta.color_labels.get(st)
                if spec is not None:
                    color = spec.xmp_label
                else:
                    raw = self._lr.color_labels.get(req.status, "none")
                    color = _lightroom_label_token(raw)

        try:
            write_sidecar(
                Path(req.file_path),
                req,
                color_label_lightroom=color,
                write_label=self._lr.enable and self._meta.write_color_label,
                overwrite_label=self._meta.overwrite_color_labels,
                write_keywords=self._meta.write_keywords,
                write_fields=self._meta.write_fields,
            )
        except OSError as e:
            self._log.warning("metadata XMP write failed for %s: %s", req.file_path, e)
        except Exception:
            self._log.exception("metadata XMP write failed for %s", req.file_path)


def build_metadata_sync(
    settings: Settings,
    log: logging.Logger | None = None,
    audit: logging.Logger | None = None,
) -> MetadataSyncPort:
    if not settings.metadata.enabled:
        return NoOpMetadataSync()
    if settings.metadata.writer == "exiftool":
        from faceit_ai.metadata.exiftool_sync import ExifToolMetadataSync

        return ExifToolMetadataSync(settings, log=log, audit=audit)
    return XmpSidecarMetadataSync(settings.metadata, settings.lightroom, log=log)
