"""Tests for multipart form parsing."""

from __future__ import annotations

from io import BytesIO

from faceit_ai.multipart_form import parse_multipart


def _build_multipart(
    fields: dict[str, str],
    files: list[tuple[str, str, bytes]] | None = None,
) -> tuple[bytes, str]:
    boundary = "----testboundary"
    body = BytesIO()
    for key, value in fields.items():
        body.write(f"--{boundary}\r\n".encode())
        body.write(f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode())
        body.write(f"{value}\r\n".encode())
    for field_name, filename, data in files or []:
        body.write(f"--{boundary}\r\n".encode())
        body.write(
            f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n'.encode()
        )
        body.write(b"Content-Type: image/jpeg\r\n\r\n")
        body.write(data)
        body.write(b"\r\n")
    body.write(f"--{boundary}--\r\n".encode())
    return body.getvalue(), f"multipart/form-data; boundary={boundary}"


def test_parse_text_fields() -> None:
    body, ctype = _build_multipart({"first_name": "Anna", "last_name": "Mueller"})
    form = parse_multipart(body, ctype)
    assert form.getfirst("first_name") == "Anna"
    assert form.getfirst("last_name") == "Mueller"
    assert form.getfirst("missing") is None


def test_parse_file_upload() -> None:
    body, ctype = _build_multipart(
        {"first_name": "Daniel"},
        files=[("photos", "portrait.jpg", b"fake-jpeg")],
    )
    form = parse_multipart(body, ctype)
    assert form.getfirst("first_name") == "Daniel"
    assert "photos" in form
    upload = form["photos"]
    assert not isinstance(upload, list)
    assert getattr(upload, "filename", None) == "portrait.jpg"
    assert upload.file.read() == b"fake-jpeg"
