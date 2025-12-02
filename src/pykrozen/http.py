"""HTTP request/response types and helpers."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, NamedTuple, TypedDict

from pykrozen.constants import (
    CONTENT_TYPE_HTML,
    CONTENT_TYPE_MULTIPART,
    CONTENT_TYPE_OCTET,
    CONTENT_TYPE_TEXT,
    ERROR_EXPECTED_MULTIPART,
    ERROR_INVALID_BODY,
    ERROR_MISSING_BOUNDARY,
    HEADER_CONTENT_DISPOSITION,
    HEADER_CONTENT_TYPE,
    MULTIPART_BOUNDARY_MARKER,
    MULTIPART_FILENAME_MARKER,
)

_EMPTY_DICT: dict[str, str] = {}


class Route(NamedTuple):
    """Route definition: method, path, handler."""

    method: str
    path: str
    handler: Any


class Response(TypedDict, total=False):
    """Route handler response dict."""

    body: Any
    status: int
    headers: dict[str, str]


class UploadedFile(TypedDict):
    """Uploaded file: filename, content_type, content."""

    filename: str
    content_type: str
    content: bytes


class Request:
    """HTTP request: method, path, headers, query, body, params, extra."""

    __slots__ = ("method", "path", "headers", "query", "body", "params", "extra")

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
        self.headers = headers or {}
        self.query = query or {}
        self.body = body
        self.params = params or {}
        self.extra = kwargs

    def __repr__(self) -> str:
        return f"Request({self.method!r}, {self.path!r})"


class ResponseHTTP:
    """Mutable HTTP response: status, body, headers, stop."""

    __slots__ = ("status", "body", "headers", "stop")

    def __init__(
        self,
        status: int = 200,
        body: Any = None,
        headers: dict[str, str] | None = None,
        stop: bool = False,
    ) -> None:
        self.status = status
        self.body = body if body is not None else {}
        self.headers = headers or {}
        self.stop = stop

    def __repr__(self) -> str:
        return f"ResponseHTTP({self.status})"


class HTTPContext(NamedTuple):
    """Context for HTTP hooks: req, res."""

    req: Request
    res: ResponseHTTP


def parse_query(path: str) -> tuple[str, dict[str, str]]:
    """Split path and query string, return (path, params)."""
    qmark = path.find("?")
    if qmark == -1:
        return path, _EMPTY_DICT
    base, qs = path[:qmark], path[qmark + 1 :]
    params = {}
    for pair in qs.split("&"):
        eq = pair.find("=")
        if eq != -1:
            params[pair[:eq]] = pair[eq + 1 :]
    return base, params


# Backwards compatibility aliases
make_request = Request
make_response = ResponseHTTP


def html(content: str, status: int = 200) -> Response:
    """Create HTML response."""
    return {
        "body": content,
        "headers": {HEADER_CONTENT_TYPE: CONTENT_TYPE_HTML},
        "status": status,
    }


def text(content: str, status: int = 200) -> Response:
    """Create plain text response."""
    return {
        "body": content,
        "headers": {HEADER_CONTENT_TYPE: CONTENT_TYPE_TEXT},
        "status": status,
    }


def error(message: str, status: int = 400) -> Response:
    """Create JSON error response."""
    return {"body": {"error": message}, "status": status}


def parse_multipart(body: bytes, boundary: bytes) -> list[UploadedFile]:
    """Parse multipart form data, extract files."""
    files: list[UploadedFile] = []
    for part in body.split(b"--" + boundary):
        if not part or part in (b"--\r\n", b"--", b"\r\n"):
            continue
        if b"\r\n\r\n" not in part:
            continue
        header_section, content = part.split(b"\r\n\r\n", 1)
        content = content.rstrip(b"\r\n")
        headers = {}
        for line in header_section.split(b"\r\n"):
            if b": " in line:
                k, v = line.split(b": ", 1)
                headers[k.decode().lower()] = v.decode()
        disposition = headers.get(HEADER_CONTENT_DISPOSITION, "")
        if MULTIPART_FILENAME_MARKER not in disposition:
            continue
        filename = disposition.split(MULTIPART_FILENAME_MARKER)[1].split('"')[0]
        files.append(
            {
                "filename": filename,
                "content_type": headers.get("content-type", CONTENT_TYPE_OCTET),
                "content": content,
            }
        )
    return files


UploadHandler = Callable[[list[UploadedFile]], Response]


def create_upload_handler(handler: UploadHandler) -> Callable[[Request], Response]:
    """Wrap upload handler for multipart requests."""

    def wrapped(req: Request) -> Response:
        ct = req.headers.get(HEADER_CONTENT_TYPE, "")
        if CONTENT_TYPE_MULTIPART not in ct:
            return error(ERROR_EXPECTED_MULTIPART)
        if MULTIPART_BOUNDARY_MARKER not in ct:
            return error(ERROR_MISSING_BOUNDARY)
        boundary = ct.split(MULTIPART_BOUNDARY_MARKER)[1].strip().encode()
        if not isinstance(req.body, bytes):
            return error(ERROR_INVALID_BODY)
        return handler(parse_multipart(req.body, boundary))

    return wrapped


# ──────────────────────────────────────────────────────────────────────────────
# Router Factory (OpenAPI/GraphQL future-proof)
# ──────────────────────────────────────────────────────────────────────────────


@dataclass
class RouteInfo:
    """Route metadata for OpenAPI: tags, summary, description, deprecated."""

    tags: list[str] = field(default_factory=list)
    summary: str = ""
    description: str = ""
    deprecated: bool = False
    operation_id: str = ""
    responses: dict[int, str] = field(default_factory=dict)


@dataclass
class Router:
    """Route group with prefix and shared metadata (OpenAPI-ready)."""

    prefix: str = ""
    tags: list[str] = field(default_factory=list)
    _routes: list[tuple[str, str, Any, RouteInfo]] = field(default_factory=list)
    _app: Any = None

    def _build_path(self, path: str) -> str:
        """Build full path from prefix and path."""
        if path == "/":
            return self.prefix or "/"
        full = f"{self.prefix.rstrip('/')}/{path.lstrip('/')}"
        return full.rstrip("/") if full != "/" else full

    def _add(
        self,
        method: str,
        path: str,
        handler: Any,
        *,
        tags: list[str] | None = None,
        summary: str = "",
        description: str = "",
        deprecated: bool = False,
        operation_id: str = "",
        responses: dict[int, str] | None = None,
    ) -> Any:
        """Register route with metadata."""
        full_path = self._build_path(path)
        info = RouteInfo(
            tags=tags or self.tags.copy(),
            summary=summary,
            description=description,
            deprecated=deprecated,
            operation_id=operation_id or handler.__name__,
            responses=responses or {},
        )
        self._routes.append((method, full_path, handler, info))
        if self._app is not None:
            self._app._register_route(method, full_path, handler)
        return handler

    def _decorator(
        self,
        method: str,
        path: str,
        tags: list[str] | None,
        summary: str,
        description: str,
        deprecated: bool,
        operation_id: str,
        responses: dict[int, str] | None,
    ) -> Callable[[Any], Any]:
        """Create route decorator."""

        def dec(fn: Any) -> Any:
            return self._add(
                method,
                path,
                fn,
                tags=tags,
                summary=summary,
                description=description,
                deprecated=deprecated,
                operation_id=operation_id,
                responses=responses,
            )

        return dec

    def get(
        self,
        path: str,
        *,
        tags: list[str] | None = None,
        summary: str = "",
        description: str = "",
        deprecated: bool = False,
        operation_id: str = "",
        responses: dict[int, str] | None = None,
    ) -> Callable[[Any], Any]:
        """GET route decorator."""
        return self._decorator(
            "GET",
            path,
            tags,
            summary,
            description,
            deprecated,
            operation_id,
            responses,
        )

    def post(
        self,
        path: str,
        *,
        tags: list[str] | None = None,
        summary: str = "",
        description: str = "",
        deprecated: bool = False,
        operation_id: str = "",
        responses: dict[int, str] | None = None,
    ) -> Callable[[Any], Any]:
        """POST route decorator."""
        return self._decorator(
            "POST",
            path,
            tags,
            summary,
            description,
            deprecated,
            operation_id,
            responses,
        )

    def put(
        self,
        path: str,
        *,
        tags: list[str] | None = None,
        summary: str = "",
        description: str = "",
        deprecated: bool = False,
        operation_id: str = "",
        responses: dict[int, str] | None = None,
    ) -> Callable[[Any], Any]:
        """PUT route decorator."""
        return self._decorator(
            "PUT",
            path,
            tags,
            summary,
            description,
            deprecated,
            operation_id,
            responses,
        )

    def delete(
        self,
        path: str,
        *,
        tags: list[str] | None = None,
        summary: str = "",
        description: str = "",
        deprecated: bool = False,
        operation_id: str = "",
        responses: dict[int, str] | None = None,
    ) -> Callable[[Any], Any]:
        """DELETE route decorator."""
        return self._decorator(
            "DELETE",
            path,
            tags,
            summary,
            description,
            deprecated,
            operation_id,
            responses,
        )

    def patch(
        self,
        path: str,
        *,
        tags: list[str] | None = None,
        summary: str = "",
        description: str = "",
        deprecated: bool = False,
        operation_id: str = "",
        responses: dict[int, str] | None = None,
    ) -> Callable[[Any], Any]:
        """PATCH route decorator."""
        return self._decorator(
            "PATCH",
            path,
            tags,
            summary,
            description,
            deprecated,
            operation_id,
            responses,
        )

    def routes(self) -> list[tuple[str, str, Any, RouteInfo]]:
        """Get all registered routes with metadata."""
        return self._routes.copy()


def router(prefix: str = "", *, tags: list[str] | None = None) -> Router:
    """Create route group with prefix and tags (OpenAPI-ready)."""
    return Router(prefix=prefix, tags=tags or [])
