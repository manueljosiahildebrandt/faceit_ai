"""Minimal multipart/form-data parser (replacement for removed cgi.FieldStorage)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from io import BytesIO
from typing import BinaryIO


@dataclass
class UploadedFile:
    filename: str | None
    _data: bytes

    @property
    def file(self) -> BinaryIO:
        return BytesIO(self._data)


FieldValue = str | UploadedFile | list[str | UploadedFile]


class MultipartForm:
    """Dict-like view of parsed multipart fields."""

    def __init__(self, fields: dict[str, FieldValue]) -> None:
        self._fields = fields

    def getfirst(self, name: str, default: str | None = None) -> str | None:
        val = self._fields.get(name)
        if val is None:
            return default
        if isinstance(val, UploadedFile):
            return default
        if isinstance(val, list):
            for item in val:
                if isinstance(item, str):
                    return item
            return default
        return val

    def __contains__(self, key: str) -> bool:
        return key in self._fields

    def __getitem__(self, key: str) -> FieldValue:
        return self._fields[key]


def _parse_content_disposition(header: str) -> tuple[str | None, str | None]:
    name_match = re.search(r"""name=(["'])(.*?)\1""", header)
    if not name_match:
        name_match = re.search(r"name=([^;]+)", header)
    name = name_match.group(2 if name_match and name_match.lastindex == 2 else 1).strip() if name_match else None
    fn_match = re.search(r"""filename=(["'])(.*?)\1""", header)
    if not fn_match:
        fn_match = re.search(r"filename=([^;]+)", header)
    filename = None
    if fn_match:
        filename = fn_match.group(2 if fn_match.lastindex == 2 else 1).strip()
    return name, filename


def parse_multipart(body: bytes, content_type: str) -> MultipartForm:
    """Parse a multipart/form-data body."""
    match = re.search(r"""boundary=(?:"([^"]+)"|([^;\s]+))""", content_type, re.I)
    if not match:
        raise ValueError("Missing multipart boundary in Content-Type.")
    boundary = (match.group(1) or match.group(2) or "").encode("latin-1")
    delimiter = b"--" + boundary
    fields: dict[str, FieldValue] = {}

    for part in body.split(delimiter):
        chunk = part.strip(b"\r\n")
        if not chunk or chunk == b"--":
            continue
        if chunk.endswith(b"--"):
            chunk = chunk[:-2].strip(b"\r\n")
        header_blob, _, content = chunk.partition(b"\r\n\r\n")
        if not header_blob:
            continue
        if content.endswith(b"\r\n"):
            content = content[:-2]

        headers: dict[str, str] = {}
        for line in header_blob.decode("latin-1", errors="replace").split("\r\n"):
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            headers[key.strip().lower()] = value.strip()

        disposition = headers.get("content-disposition", "")
        name, filename = _parse_content_disposition(disposition)
        if not name:
            continue

        if filename is not None:
            value: str | UploadedFile = UploadedFile(filename=filename or None, _data=content)
        else:
            charset = "utf-8"
            ct = headers.get("content-type", "")
            ct_match = re.search(r"charset=([^;\s]+)", ct, re.I)
            if ct_match:
                charset = ct_match.group(1).strip()
            value = content.decode(charset, errors="replace")

        existing = fields.get(name)
        if existing is None:
            fields[name] = value
        elif isinstance(existing, list):
            existing.append(value)
        else:
            fields[name] = [existing, value]

    return MultipartForm(fields)
