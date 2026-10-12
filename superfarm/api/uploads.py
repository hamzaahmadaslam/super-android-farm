"""Receiving a file from a client without ever holding it in memory.

An upload (an app, a video, a file to push) goes straight from the socket to a temporary
file under `<home>/run/uploads`, and is cut off the moment it passes the size limit: the
limit is enforced on the bytes that arrive, not on what the client says it will send.

Two request shapes are accepted, so a browser form and `curl --data-binary` both work:

    multipart/form-data            one file part (any field name) plus small text fields
    anything else (raw body)       the file is the body; its name is `?filename=`

The client's file name only ever passes through `safe_filename`; the temporary file has a
random name of ours.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import python_multipart
from python_multipart.multipart import parse_options_header
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request

from ..farm import Farm
from ..util.files import safe_filename
from .errors import ApiError

MAX_FIELD_BYTES = 64 * 1024        # text fields next to the file (selector, target path, ...)
MAX_FIELDS = 16
_ENVELOPE_SLACK = 1024 * 1024      # multipart boundaries and headers around the file itself


@dataclass
class Upload:
    path: Path
    filename: str                  # the client's name, reduced to a plain safe file name
    size: int
    fields: dict[str, str] = field(default_factory=dict)
    _folder: Path | None = None

    def named(self) -> Path:
        """Give the file the client's (safe) name, in a folder of its own, for callers that
        derive a destination name from the local one. Returns the new path."""
        if self._folder is None:
            folder = self.path.with_name(self.path.name + ".d")
            folder.mkdir(mode=0o700)
            target = folder / self.filename
            self.path.rename(target)
            self.path, self._folder = target, folder
        return self.path

    def discard(self) -> None:
        self.path.unlink(missing_ok=True)
        if self._folder is not None:
            shutil.rmtree(self._folder, ignore_errors=True)


def uploads_dir(farm: Farm) -> Path:
    folder = farm.paths.run / "uploads"
    folder.mkdir(parents=True, exist_ok=True)
    os.chmod(folder, 0o700)
    return folder


def sweep(farm: Farm) -> int:
    """Remove what an earlier daemon left behind. Called once at start, before any upload."""
    removed = 0
    for path in uploads_dir(farm).iterdir():
        if path.is_symlink() or path.is_file():
            path.unlink(missing_ok=True)
        else:
            shutil.rmtree(path, ignore_errors=True)
        removed += 1
    return removed


def _too_large(limit: int) -> ApiError:
    return ApiError(413, "too_large", f"the upload is larger than {max(1, limit // (1024 * 1024))} MB "
                                      "(server.max_upload_mb)", headers={"Connection": "close"})


class _Multipart:
    """Callbacks for python-multipart: file bytes are queued for the caller to write, text
    fields are collected with a small bound."""

    def __init__(self, limit: int):
        self.limit = limit
        self.fields: dict[str, str] = {}
        self.filename: str | None = None
        self.file_bytes = 0
        self.pending: list[bytes] = []
        self._header_name = b""
        self._header_value = b""
        self._disposition = b""
        self._field: str | None = None
        self._value = bytearray()
        self._in_file = False

    def on_part_begin(self) -> None:
        self._disposition, self._field, self._in_file = b"", None, False
        self._value = bytearray()

    def on_header_field(self, data: bytes, start: int, end: int) -> None:
        self._header_name += data[start:end]

    def on_header_value(self, data: bytes, start: int, end: int) -> None:
        self._header_value += data[start:end]
        if len(self._header_value) > MAX_FIELD_BYTES:
            raise ApiError(400, "bad_request", "malformed upload: a part header is too long")

    def on_header_end(self) -> None:
        if self._header_name.lower() == b"content-disposition":
            self._disposition = self._header_value
        self._header_name, self._header_value = b"", b""

    def on_headers_finished(self) -> None:
        _, options = parse_options_header(self._disposition)
        name = options.get(b"name", b"").decode("utf-8", "replace")
        if b"filename" in options:
            if self.filename is not None:
                raise ApiError(400, "bad_request", "one file per upload")
            self.filename = options[b"filename"].decode("utf-8", "replace")
            self._in_file = True
            return
        if not name or len(self.fields) >= MAX_FIELDS:
            raise ApiError(400, "bad_request", "malformed upload: unnamed or too many form fields")
        self._field = name

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        chunk = data[start:end]
        if self._in_file:
            self.file_bytes += len(chunk)
            if self.file_bytes > self.limit:
                raise _too_large(self.limit)
            self.pending.append(chunk)
        elif self._field is not None:
            if len(self._value) + len(chunk) > MAX_FIELD_BYTES:
                raise ApiError(400, "bad_request", f"form field {self._field[:40]!r} is too long")
            self._value.extend(chunk)

    def on_part_end(self) -> None:
        if self._field is not None:
            self.fields[self._field] = self._value.decode("utf-8", "replace")

    def callbacks(self) -> dict:
        return {name: getattr(self, name) for name in (
            "on_part_begin", "on_part_data", "on_part_end", "on_header_field", "on_header_value",
            "on_header_end", "on_headers_finished")}


async def receive_upload(request: Request, farm: Farm, *, max_bytes: int | None = None,
                         default_name: str = "upload") -> Upload:
    """Stream the request's file to a temporary file and return where it is.

    The caller owns the result and must `discard()` it (or move the file away). Raises
    ApiError 413 as soon as the file passes `server.max_upload_mb` (or `max_bytes` when lower).
    """
    limit = farm.config.server.max_upload_mb * 1024 * 1024
    if max_bytes is not None:
        limit = min(limit, int(max_bytes))
    content_type, options = parse_options_header(request.headers.get("content-type", ""))
    multipart = content_type == b"multipart/form-data"
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit + (_ENVELOPE_SLACK if multipart else 0):
        raise _too_large(limit)

    form: _Multipart | None = None
    parser = None
    if multipart:
        boundary = options.get(b"boundary")
        if not boundary:
            raise ApiError(400, "bad_request", "malformed upload: multipart body without a boundary")
        form = _Multipart(limit)
        parser = python_multipart.MultipartParser(boundary, form.callbacks())

    fd, name = tempfile.mkstemp(dir=uploads_dir(farm), prefix="up-")      # 0600, random name
    path = Path(name)
    received = written = 0
    try:
        with os.fdopen(fd, "wb") as out:
            async for chunk in request.stream():
                if not chunk:
                    continue
                received += len(chunk)
                if form is None:
                    if received > limit:
                        raise _too_large(limit)
                    await run_in_threadpool(out.write, chunk)
                    written += len(chunk)
                    continue
                if received > limit + _ENVELOPE_SLACK:
                    raise _too_large(limit)
                try:
                    parser.write(chunk)
                except python_multipart.exceptions.FormParserError:
                    raise ApiError(400, "bad_request", "malformed upload: the multipart body could not be read") from None
                if form.pending:
                    data, form.pending = b"".join(form.pending), []
                    await run_in_threadpool(out.write, data)
                    written += len(data)
            if parser is not None:
                try:
                    parser.finalize()
                except python_multipart.exceptions.FormParserError:
                    raise ApiError(400, "bad_request", "the upload ended before it was complete") from None
        if form is not None and form.filename is None:
            raise ApiError(400, "bad_request", "the form has no file part")
        if written == 0:
            raise ApiError(400, "bad_request", "the uploaded file is empty")
        client_name = form.filename if form is not None else request.query_params.get("filename", "")
        return Upload(path=path, filename=safe_filename(client_name or "", default_name), size=written,
                      fields=dict(form.fields) if form is not None else {})
    except BaseException:
        path.unlink(missing_ok=True)
        raise
