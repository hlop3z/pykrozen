"""HTTP request/response handling and helpers."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any, NamedTuple, TypedDict

from pykrozen.constants import (
    CONTENT_TYPE_HTML,
    CONTENT_TYPE_MULTIPART,
    CONTENT_TYPE_OCTET,
    CONTENT_TYPE_TEXT,
    ERROR_EXPECTED_MULTIPART,
    ERROR_FILE_NOT_FOUND,
    ERROR_INVALID_BODY,
    ERROR_MISSING_BOUNDARY,
    HEADER_CONTENT_DISPOSITION,
    HEADER_CONTENT_TYPE,
    MIME_TYPES,
    MULTIPART_BOUNDARY_MARKER,
    MULTIPART_FILENAME_MARKER,
)

# Empty dict singleton to avoid allocations
_EMPTY_DICT: dict[str, str] = {}


class Route(NamedTuple):
    """Route definition."""

    method: str
    path: str
    handler: Any


class ResponseDict(TypedDict, total=False):
    """Route handler response: body, status, headers (all optional)."""

    body: Any
    status: int
    headers: dict[str, str]


class UploadedFile(TypedDict):
    """Uploaded file from multipart form data: filename, content_type, content."""

    filename: str
    content_type: str
    content: bytes


class Request:
    """HTTP request context: method, path, headers, query, body, params, extra."""

    __slots__ = ("method", "path", "headers", "query", "body", "params", "extra")

    method: str
    path: str
    headers: dict[str, str]
    query: dict[str, str]
    body: Any
    params: dict[str, str]
    extra: dict[str, Any]

    def __init__(
        self,
        method: str = "",
        path: str = "",
        headers: dict[str, str] | None = None,
        query: dict[str, str] | None = None,
        body: Any = None,
        params: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> None:
        self.method = method
        self.path = path
        self.headers = headers if headers is not None else {}
        self.query = query if query is not None else {}
        self.body = body
        self.params = params if params is not None else {}
        self.extra = kwargs

    def __repr__(self) -> str:
        return f"Request(method={self.method!r}, path={self.path!r})"


class Response:
    """HTTP response context: status, body, headers, stop."""

    __slots__ = ("status", "body", "headers", "stop")

    status: int
    body: Any
    headers: dict[str, str]
    stop: bool

    def __init__(
        self,
        status: int = 200,
        body: Any = None,
        headers: dict[str, str] | None = None,
        stop: bool = False,
    ) -> None:
        self.status = status
        self.body = body if body is not None else {}
        self.headers = headers if headers is not None else {}
        self.stop = stop

    def __repr__(self) -> str:
        return f"Response(status={self.status})"


class HTTPContext(NamedTuple):
    """Context passed to HTTP hooks."""

    req: Request
    res: Response


def parse_query(path: str) -> tuple[str, dict[str, str]]:
    """Parse query string from path."""
    qmark = path.find("?")
    if qmark == -1:
        return path, _EMPTY_DICT
    base = path[:qmark]
    qs = path[qmark + 1 :]
    params = {}
    for pair in qs.split("&"):
        eq = pair.find("=")
        if eq != -1:
            params[pair[:eq]] = pair[eq + 1 :]
    return base, params


# Factory aliases (for backwards compatibility)
make_request = Request
make_response = Response


def html(content: str, status: int = 200) -> ResponseDict:
    """Create an HTML response."""
    return {
        "body": content,
        "headers": {HEADER_CONTENT_TYPE: CONTENT_TYPE_HTML},
        "status": status,
    }


def text(content: str, status: int = 200) -> ResponseDict:
    """Create a plain text response."""
    return {
        "body": content,
        "headers": {HEADER_CONTENT_TYPE: CONTENT_TYPE_TEXT},
        "status": status,
    }


def error(message: str, status: int = 400) -> ResponseDict:
    """Create an error response with JSON body `{"error": message}`."""
    return {"body": {"error": message}, "status": status}


def file(
    filepath: Path, content_type: str | None = None, status: int = 200
) -> ResponseDict:
    """Create a file response (auto-detects MIME type). Returns 404 if not found."""
    _filepath = str(filepath)
    try:
        with open(_filepath, "rb") as f:
            content = f.read()
    except FileNotFoundError:
        return error(ERROR_FILE_NOT_FOUND, 404)

    if content_type is None:
        ext = os.path.splitext(_filepath)[1].lower()
        content_type = MIME_TYPES.get(ext, CONTENT_TYPE_OCTET)

    return {
        "body": content,
        "headers": {HEADER_CONTENT_TYPE: content_type},
        "status": status,
    }


def parse_multipart(body: bytes, boundary: bytes) -> list[UploadedFile]:
    """Parse multipart form data and extract files."""
    files: list[UploadedFile] = []
    parts = body.split(b"--" + boundary)

    for part in parts:
        if not part or part in (b"--\r\n", b"--", b"\r\n"):
            continue

        if b"\r\n\r\n" not in part:
            continue

        header_section, content = part.split(b"\r\n\r\n", 1)
        content = content.rstrip(b"\r\n")

        headers: dict[str, str] = {}
        for line in header_section.split(b"\r\n"):
            if b": " in line:
                key, val = line.split(b": ", 1)
                headers[key.decode().lower()] = val.decode()

        disposition = headers.get(HEADER_CONTENT_DISPOSITION, "")
        if MULTIPART_FILENAME_MARKER not in disposition:
            continue

        filename = disposition.split(MULTIPART_FILENAME_MARKER)[1].split('"')[0]
        file_content_type = headers.get("content-type", CONTENT_TYPE_OCTET)

        files.append(
            {
                "filename": filename,
                "content_type": file_content_type,
                "content": content,
            }
        )

    return files


UploadHandler = Callable[[list[UploadedFile]], ResponseDict]


def create_upload_handler(handler: UploadHandler) -> Callable[[Request], ResponseDict]:
    """Create an upload endpoint handler for multipart file uploads."""

    def upload_handler(req: Request) -> ResponseDict:
        content_type = req.headers.get(HEADER_CONTENT_TYPE, "")
        if CONTENT_TYPE_MULTIPART not in content_type:
            return error(ERROR_EXPECTED_MULTIPART)

        if MULTIPART_BOUNDARY_MARKER not in content_type:
            return error(ERROR_MISSING_BOUNDARY)

        boundary = content_type.split(MULTIPART_BOUNDARY_MARKER)[1].strip().encode()
        if not isinstance(req.body, bytes):
            return error(ERROR_INVALID_BODY)

        files = parse_multipart(req.body, boundary)
        result: ResponseDict = handler(files)
        return result

    return upload_handler
