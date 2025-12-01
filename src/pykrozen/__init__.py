"""
Lightweight HTTP + WebSocket server with plugin system.

A minimal, zero-dependency HTTP and WebSocket server designed for
development and prototyping. Features middleware, routing, plugins,
and WebSocket support out of the box.

Example:
    >>> from pykrozen import Server, get
    >>> @get("/hello")
    ... def hello(req):
    ...     return {"message": "Hello, World!"}
    >>> Server.run(port=8000)
"""

from __future__ import annotations

__version__ = "0.1.0"
__all__ = [
    # Version
    "__version__",
    # Core classes
    "Server",
    "AsyncServer",
    "App",
    "Plugin",
    # Request/Response
    "Request",
    "Response",
    "ResponseDict",
    "UploadedFile",
    # WebSocket
    "WSClient",
    "WSMessage",
    "WSContext",
    # Context types
    "HTTPContext",
    "ServerContext",
    "Route",
    # Global app and decorators
    "app",
    "get",
    "post",
    "put",
    "delete",
    "patch",
    "on",
    "use",
    # Factory functions
    "make_request",
    "make_response",
    # Response helpers
    "html",
    "text",
    "file",
    "upload",
    # Type aliases
    "MiddlewareFn",
    "RouteHandler",
    "HookHandler",
    "UploadHandler",
    "PluginProtocol",
    # Performance introspection
    "get_backends",
    # Router
    "RadixRouter",
    "RouteMatch",
]

import asyncio
import contextlib
import inspect
import json
import os
import socket
import struct
import threading
from base64 import b64encode
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from hashlib import sha1
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from typing import Any, NamedTuple, Protocol, TypedDict

from pykrozen.router import RadixRouter, RouteMatch

# ──────────────────────────────────────────────────────────────────────────────
# Performance: Pre-computed constants and caches
# ──────────────────────────────────────────────────────────────────────────────

# Pre-encode common response components
_JSON_CONTENT_TYPE = b"application/json"
_HTTP_200 = b"HTTP/1.1 200 OK\r\n"
_HTTP_201 = b"HTTP/1.1 201 Created\r\n"
_CRLF = b"\r\n"

# ──────────────────────────────────────────────────────────────────────────────
# Optional Performance Backends (orjson, numpy)
# ──────────────────────────────────────────────────────────────────────────────

# Try to import orjson for faster JSON serialization (~2-3x speedup)
_orjson: Any = None
try:
    import orjson as _orjson_module

    _orjson = _orjson_module

    def _json_dumps(obj: Any) -> str:
        result: str = _orjson.dumps(obj).decode("utf-8")
        return result

    def _json_loads(data: bytes | str) -> Any:
        return _orjson.loads(data)

    _JSON_BACKEND = "orjson"
except ImportError:

    def _json_dumps(obj: Any) -> str:
        return json.dumps(obj, separators=(",", ":"))

    def _json_loads(data: bytes | str) -> Any:
        return json.loads(data)

    _JSON_BACKEND = "json"

# Try to import numpy for faster XOR masking (~10x speedup for large payloads)
_np: Any = None
try:
    import numpy as _np_module

    _np = _np_module

    def _xor_unmask_fast(payload: bytearray, mask: bytes) -> bytes:
        """Vectorized XOR unmask using numpy."""
        payload_arr = _np.frombuffer(payload, dtype=_np.uint8)
        mask_arr = _np.frombuffer(mask, dtype=_np.uint8)
        # Tile mask to match payload length and XOR
        tiled_mask = _np.tile(mask_arr, (len(payload) + 3) // 4)[: len(payload)]
        return bytes(_np.bitwise_xor(payload_arr, tiled_mask))

    _XOR_BACKEND = "numpy"
except ImportError:

    def _xor_unmask_fast(payload: bytearray, mask: bytes) -> bytes:
        """Pure Python XOR unmask (optimized)."""
        m0, m1, m2, m3 = mask[0], mask[1], mask[2], mask[3]
        mask_cycle = (m0, m1, m2, m3)
        for i in range(len(payload)):
            payload[i] ^= mask_cycle[i & 3]
        return bytes(payload)

    _XOR_BACKEND = "python"


# Try to import aiohttp for async HTTP server (~3-10x throughput improvement)
_aiohttp_web: Any = None
try:
    import aiohttp.web as _aiohttp_web_module

    _aiohttp_web = _aiohttp_web_module
    _ASYNC_BACKEND = "aiohttp"
except ImportError:
    _ASYNC_BACKEND = "threading"


def get_backends() -> dict[str, str]:
    """Return the active performance backends.

    Returns:
        dict with 'json', 'xor', and 'server' keys indicating which backend is active.

    Example:
        >>> from pykrozen import get_backends
        >>> get_backends()
        {'json': 'orjson', 'xor': 'numpy', 'server': 'aiohttp'}  # if all installed
        {'json': 'json', 'xor': 'python', 'server': 'threading'}   # stdlib fallback
    """
    return {"json": _JSON_BACKEND, "xor": _XOR_BACKEND, "server": _ASYNC_BACKEND}


# Pre-computed status lines for common codes
_STATUS_LINES: dict[int, bytes] = {
    200: b"HTTP/1.1 200 OK\r\n",
    201: b"HTTP/1.1 201 Created\r\n",
    204: b"HTTP/1.1 204 No Content\r\n",
    400: b"HTTP/1.1 400 Bad Request\r\n",
    401: b"HTTP/1.1 401 Unauthorized\r\n",
    403: b"HTTP/1.1 403 Forbidden\r\n",
    404: b"HTTP/1.1 404 Not Found\r\n",
    500: b"HTTP/1.1 500 Internal Server Error\r\n",
}

WS_MAGIC = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

# Pre-computed hook keys to avoid string operations per-request
_HTTP_HOOK_KEYS: dict[str, str] = {
    "GET": "http:get",
    "POST": "http:post",
    "PUT": "http:put",
    "DELETE": "http:delete",
    "PATCH": "http:patch",
}

# ──────────────────────────────────────────────────────────────────────────────
# Async Support
# ──────────────────────────────────────────────────────────────────────────────

# Thread-local event loop for running async handlers in sync context
_thread_local = threading.local()


def _get_event_loop() -> asyncio.AbstractEventLoop:
    """Get or create an event loop for the current thread."""
    try:
        loop = getattr(_thread_local, "loop", None)
        if loop is None or loop.is_closed():
            loop = asyncio.new_event_loop()
            _thread_local.loop = loop
        return loop
    except Exception:
        return asyncio.new_event_loop()


def _run_sync(coro: Coroutine[Any, Any, Any]) -> Any:
    """Run a coroutine synchronously in the current thread."""
    loop = _get_event_loop()
    return loop.run_until_complete(coro)


def _is_coroutine_function(func: Callable[..., Any]) -> bool:
    """Check if a function is a coroutine function."""
    return inspect.iscoroutinefunction(func)


# ──────────────────────────────────────────────────────────────────────────────
# Type Definitions
# ──────────────────────────────────────────────────────────────────────────────


class Route(NamedTuple):
    """Route definition."""

    method: str
    path: str
    handler: Callable[..., Any]


class ResponseDict(TypedDict, total=False):
    """TypedDict for route handler responses.

    All fields are optional. Defaults:
        - body: {} (empty dict, serialized as JSON)
        - status: 200
        - headers: {} (Content-Type defaults to application/json)

    Example:
        >>> @get("/page")
        ... def page(req) -> ResponseDict:
        ...     return {"body": "<h1>Hi</h1>", "headers": {"Content-Type": "text/html"}}
        >>>
        >>> @get("/api")
        ... def api(req) -> ResponseDict:
        ...     return {"body": {"message": "Hello"}, "status": 200}
    """

    body: Any  # Response body: dict/list (JSON), str, or bytes
    status: int  # HTTP status code (default: 200)
    headers: dict[str, str]  # Response headers. Set Content-Type to change format


class UploadedFile(TypedDict):
    """TypedDict representing an uploaded file.

    Example:
        >>> def handle_upload(files: list[UploadedFile]) -> ResponseDict:
        ...     for f in files:
        ...         print(f["filename"], len(f["content"]), "bytes")
        ...     return {"body": {"uploaded": len(files)}}
    """

    filename: str  # Original filename from the upload
    content_type: str  # MIME type of the file
    content: bytes  # Raw file content as bytes


class Request:
    """HTTP request context with optimized memory layout.

    Uses __slots__ for memory efficiency. Core attributes are fixed,
    but 'extra' dict allows custom data attachment.

    Attributes:
        method: HTTP method (GET, POST, etc.)
        path: URL path without query string
        headers: Request headers
        query: Parsed query string parameters
        body: Request body (parsed JSON for POST, raw bytes for multipart)
        params: URL parameters from dynamic routes (e.g., {"id": "123"} for /users/:id)
        extra: Additional data attached by middleware
    """

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
        self.extra = kwargs  # Store any additional kwargs in extra

    def __repr__(self) -> str:
        return f"Request(method={self.method!r}, path={self.path!r})"


class Response:
    """HTTP response context with optimized memory layout.

    Uses __slots__ for memory efficiency. Set stop=True to halt middleware.
    """

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


# ──────────────────────────────────────────────────────────────────────────────
# WebSocket Client
# ──────────────────────────────────────────────────────────────────────────────


@dataclass
class WSClient:
    """WebSocket client connection. Use 'data' namespace for per-connection state."""

    sock: socket.socket
    data: SimpleNamespace = field(default_factory=SimpleNamespace)

    def handshake(self) -> None:
        """Perform WebSocket handshake."""
        req = self.sock.recv(1024)
        key = next(
            line.split(b": ", 1)[1]
            for line in req.split(b"\r\n")
            if line.lower().startswith(b"sec-websocket-key")
        )
        accept = b64encode(sha1(key + WS_MAGIC).digest())
        self.sock.send(
            b"HTTP/1.1 101 Switching Protocols\r\n"
            b"Upgrade: websocket\r\nConnection: Upgrade\r\n"
            b"Sec-WebSocket-Accept: " + accept + b"\r\n\r\n"
        )

    def recv(self) -> tuple[int | None, bytes | None]:
        """Receive a WebSocket frame. Returns (opcode, data)."""
        header = self.sock.recv(2)
        if len(header) < 2:
            return None, None

        opcode = header[0] & 0x0F
        length = header[1] & 0x7F

        if length == 126:
            length = struct.unpack(">H", self.sock.recv(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self.sock.recv(8))[0]

        if header[1] & 0x80:
            mask = self.sock.recv(4)
            # Read payload in chunks for large messages
            payload = bytearray()
            remaining = length
            while remaining > 0:
                chunk = self.sock.recv(min(remaining, 65536))
                if not chunk:
                    break
                payload.extend(chunk)
                remaining -= len(chunk)
            # XOR unmask (uses numpy if available, else optimized pure Python)
            data = _xor_unmask_fast(payload, mask)
        else:
            data = self.sock.recv(length)

        return opcode, data

    def send(self, data: Any, opcode: int = 1) -> None:
        """Send data over WebSocket. Auto-serializes dicts to JSON."""
        if opcode == 1 and not isinstance(data, (str, bytes)):
            data = _json_dumps(data)
        payload = data.encode() if isinstance(data, str) else data
        length = len(payload)

        if length < 126:
            header = bytes([0x80 | opcode, length])
        elif length < 0x10000:
            header = bytes([0x80 | opcode, 126]) + struct.pack(">H", length)
        else:
            header = bytes([0x80 | opcode, 127]) + struct.pack(">Q", length)

        self.sock.send(header + payload)

    def close(self) -> None:
        """Close the connection."""
        self.sock.close()


class WSMessage:
    """WebSocket message context with optimized memory layout.

    Uses __slots__ for memory efficiency.
    """

    __slots__ = ("ws", "data", "reply", "stop")

    ws: WSClient
    data: Any
    reply: Any
    stop: bool

    def __init__(
        self,
        ws: WSClient,
        data: Any = None,
        reply: Any = None,
        stop: bool = False,
    ) -> None:
        self.ws = ws
        self.data = data
        self.reply = reply
        self.stop = stop

    def __repr__(self) -> str:
        return f"WSMessage(data={self.data!r})"


class WSContext(NamedTuple):
    """WebSocket connect/disconnect context."""

    ws: WSClient


class ServerContext(NamedTuple):
    """Server lifecycle context."""

    server: Server


# ──────────────────────────────────────────────────────────────────────────────
# Plugin Protocol
# ──────────────────────────────────────────────────────────────────────────────


class PluginProtocol(Protocol):
    """Protocol for plugins. Implement setup() to register hooks/routes."""

    name: str

    def setup(self, app: App) -> None: ...


# ──────────────────────────────────────────────────────────────────────────────
# Type Aliases
# ──────────────────────────────────────────────────────────────────────────────

# Sync and async route handler return types
RouteResult = Response | ResponseDict | None
AsyncRouteResult = Coroutine[Any, Any, RouteResult]

MiddlewareFn = Callable[[Request, Response], None]
RouteHandler = Callable[[Request], RouteResult | AsyncRouteResult]
HookHandler = Callable[[Any], None]
UploadHandler = Callable[[list[UploadedFile]], ResponseDict]


# ──────────────────────────────────────────────────────────────────────────────
# App
# ──────────────────────────────────────────────────────────────────────────────


@dataclass
class App:
    """Central registry for plugins, hooks, routes, and middleware.

    Uses a high-performance radix tree router for O(path_length) route matching
    with support for dynamic parameters (:id) and wildcards (*path).

    Example:
        >>> from pykrozen import app, get
        >>>
        >>> @get("/users/:id")
        ... def get_user(req):
        ...     user_id = req.params["id"]  # Extract route parameter
        ...     return {"body": {"id": user_id}}
        >>>
        >>> @get("/files/*filepath")
        ... def serve_file(req):
        ...     filepath = req.params["filepath"]  # Wildcard captures rest of path
        ...     return {"body": {"file": filepath}}
    """

    _hooks: dict[str, list[HookHandler]] = field(default_factory=dict)
    _middleware: list[MiddlewareFn] = field(default_factory=list)
    _router: RadixRouter = field(default_factory=RadixRouter)
    # Legacy routes dict for backwards compatibility
    _routes: dict[str, dict[str, RouteHandler]] = field(
        default_factory=lambda: {
            "GET": {},
            "POST": {},
            "PUT": {},
            "DELETE": {},
            "PATCH": {},
        }
    )
    _plugins: list[PluginProtocol] = field(default_factory=list)
    state: SimpleNamespace = field(default_factory=SimpleNamespace)

    def plugin(self, p: PluginProtocol) -> App:
        """Register a plugin."""
        self._plugins.append(p)
        p.setup(self)
        return self

    def on(self, event: str) -> Callable[[HookHandler], HookHandler]:
        """Decorator to register an event hook."""

        def decorator(fn: HookHandler) -> HookHandler:
            self._hooks.setdefault(event, []).append(fn)
            return fn

        return decorator

    def use(self, fn: MiddlewareFn) -> MiddlewareFn:
        """Register middleware. Runs on every HTTP request."""
        self._middleware.append(fn)
        return fn

    def _register_route(self, method: str, path: str, fn: RouteHandler) -> RouteHandler:
        """Register a route with both radix router and legacy dict."""
        self._router.add(method, path, fn)
        self._routes.setdefault(method, {})[path] = fn
        return fn

    def get(self, path: str) -> Callable[[RouteHandler], RouteHandler]:
        """Decorator to register a GET route handler.

        Supports dynamic parameters with :param syntax and wildcards with *name.

        Example:
            >>> @get("/users")
            ... def list_users(req): ...
            >>>
            >>> @get("/users/:id")
            ... def get_user(req):
            ...     return {"body": {"id": req.params["id"]}}
            >>>
            >>> @get("/files/*path")
            ... def serve_file(req):
            ...     return {"body": {"path": req.params["path"]}}
        """

        def decorator(fn: RouteHandler) -> RouteHandler:
            return self._register_route("GET", path, fn)

        return decorator

    def post(self, path: str) -> Callable[[RouteHandler], RouteHandler]:
        """Decorator to register a POST route handler."""

        def decorator(fn: RouteHandler) -> RouteHandler:
            return self._register_route("POST", path, fn)

        return decorator

    def put(self, path: str) -> Callable[[RouteHandler], RouteHandler]:
        """Decorator to register a PUT route handler."""

        def decorator(fn: RouteHandler) -> RouteHandler:
            return self._register_route("PUT", path, fn)

        return decorator

    def delete(self, path: str) -> Callable[[RouteHandler], RouteHandler]:
        """Decorator to register a DELETE route handler."""

        def decorator(fn: RouteHandler) -> RouteHandler:
            return self._register_route("DELETE", path, fn)

        return decorator

    def patch(self, path: str) -> Callable[[RouteHandler], RouteHandler]:
        """Decorator to register a PATCH route handler."""

        def decorator(fn: RouteHandler) -> RouteHandler:
            return self._register_route("PATCH", path, fn)

        return decorator

    def emit(self, event: str, ctx: Any) -> Any:
        """Emit an event to all registered hooks."""
        for fn in self._hooks.get(event, []):
            fn(ctx)
            if getattr(ctx, "stop", False):
                break
        return ctx

    def run_middleware(self, req: Request, res: Response) -> bool:
        """Run all middleware. Returns False if chain was stopped."""
        for fn in self._middleware:
            fn(req, res)
            if res.stop:
                return False
        return True

    def route(self, method: str, path: str, req: Request) -> ResponseDict | None:
        """Find and execute a route handler using radix tree matching.

        Automatically extracts URL parameters and attaches them to req.params.
        """
        # Use radix router for matching (supports :param and *wildcard)
        match = self._router.match_new(method, path)
        if not match.matched or match.handler is None:
            return None

        # Attach extracted parameters to request
        if match.params:
            req.params.update(match.params)

        result = match.handler(req)
        if isinstance(result, Response):
            return {
                "status": result.status,
                "body": result.body,
                "headers": result.headers,
            }
        response: ResponseDict | None = result
        return response


@dataclass
class Plugin:
    """Base plugin class. Subclass and override setup()."""

    name: str = "plugin"

    def setup(self, app: App) -> None:
        pass


# Global app instance and convenience decorators
app = App()
on = app.on
use = app.use
get = app.get
post = app.post
put = app.put
delete = app.delete
patch = app.patch


# ──────────────────────────────────────────────────────────────────────────────
# HTTP Handling
# ──────────────────────────────────────────────────────────────────────────────


# Empty dict singleton to avoid allocations
_EMPTY_DICT: dict[str, str] = {}
_EMPTY_HEADERS: dict[str, str] = {}


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


def make_request(
    method: str,
    path: str,
    headers: dict[str, str],
    query: dict[str, str],
    body: Any = None,
) -> Request:
    """Create a Request object."""
    return Request(method=method, path=path, headers=headers, query=query, body=body)


def make_response(
    status: int = 200, body: Any = None, headers: dict[str, str] | None = None
) -> Response:
    """Create a Response object."""
    return Response(
        status=status,
        body=body if body is not None else {},
        headers=headers if headers is not None else {},
        stop=False,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Response Helpers
# ──────────────────────────────────────────────────────────────────────────────

# MIME types for common file extensions
MIME_TYPES: dict[str, str] = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".webp": "image/webp",
    ".pdf": "application/pdf",
    ".txt": "text/plain; charset=utf-8",
    ".xml": "application/xml; charset=utf-8",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
}


def html(content: str, status: int = 200) -> ResponseDict:
    """Create an HTML response.

    Args:
        content: HTML string to return.
        status: HTTP status code (default: 200).

    Returns:
        ResponseDict with body, headers, and status.

    Example:
        >>> @get("/page")
        ... def page(req):
        ...     return html("<h1>Hello World</h1>")
    """
    return {
        "body": content,
        "headers": {"Content-Type": "text/html; charset=utf-8"},
        "status": status,
    }


def text(content: str, status: int = 200) -> ResponseDict:
    """Create a plain text response.

    Args:
        content: Text string to return.
        status: HTTP status code (default: 200).

    Returns:
        ResponseDict with body, headers, and status.

    Example:
        >>> @get("/robots.txt")
        ... def robots(req):
        ...     return text("User-agent: *\\nDisallow:")
    """
    return {
        "body": content,
        "headers": {"Content-Type": "text/plain; charset=utf-8"},
        "status": status,
    }


def file(
    filepath: str, content_type: str | None = None, status: int = 200
) -> ResponseDict:
    """Create a file response by reading from disk.

    Args:
        filepath: Path to the file to serve.
        content_type: MIME type (auto-detected from extension if not provided).
        status: HTTP status code (default: 200).

    Returns:
        ResponseDict with body (bytes), headers, and status.
        Returns 404 response if file not found.

    Example:
        >>> @get("/logo")
        ... def logo(req):
        ...     return file("static/logo.png")
        >>>
        >>> @get("/download")
        ... def download(req):
        ...     return file("data/report.pdf", "application/pdf")
    """
    try:
        with open(filepath, "rb") as f:
            content = f.read()
    except FileNotFoundError:
        return {"body": {"error": "File not found"}, "status": 404}

    if content_type is None:
        ext = os.path.splitext(filepath)[1].lower()
        content_type = MIME_TYPES.get(ext, "application/octet-stream")

    return {
        "body": content,
        "headers": {"Content-Type": content_type},
        "status": status,
    }


def _parse_multipart(body: bytes, boundary: bytes) -> list[UploadedFile]:
    """Parse multipart form data and extract files."""
    files: list[UploadedFile] = []
    parts = body.split(b"--" + boundary)

    for part in parts:
        if not part or part in (b"--\r\n", b"--", b"\r\n"):
            continue

        # Split headers from content
        if b"\r\n\r\n" not in part:
            continue

        header_section, content = part.split(b"\r\n\r\n", 1)
        content = content.rstrip(b"\r\n")

        # Parse headers
        headers: dict[str, str] = {}
        for line in header_section.split(b"\r\n"):
            if b": " in line:
                key, val = line.split(b": ", 1)
                headers[key.decode().lower()] = val.decode()

        # Extract filename from Content-Disposition
        disposition = headers.get("content-disposition", "")
        if 'filename="' not in disposition:
            continue

        filename = disposition.split('filename="')[1].split('"')[0]
        content_type = headers.get("content-type", "application/octet-stream")

        files.append(
            {
                "filename": filename,
                "content_type": content_type,
                "content": content,
            }
        )

    return files


def upload(path: str, handler: UploadHandler) -> None:
    """Register an upload endpoint that handles multipart file uploads.

    Args:
        path: URL path for the upload endpoint.
        handler: Function that receives list of UploadedFile and returns ResponseDict.

    Example:
        >>> def save_files(files: list[UploadedFile]) -> ResponseDict:
        ...     for f in files:
        ...         with open(f"uploads/{f['filename']}", "wb") as out:
        ...             out.write(f["content"])
        ...     return {"body": {"uploaded": len(files)}}
        >>>
        >>> upload("/api/upload", save_files)
    """

    def upload_handler(req: Request) -> ResponseDict:
        content_type = req.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            return {"body": {"error": "Expected multipart/form-data"}, "status": 400}

        # Extract boundary
        if "boundary=" not in content_type:
            return {"body": {"error": "Missing boundary"}, "status": 400}

        boundary = content_type.split("boundary=")[1].strip().encode()
        if not isinstance(req.body, bytes):
            return {"body": {"error": "Invalid body"}, "status": 400}

        files = _parse_multipart(req.body, boundary)
        return handler(files)

    app._routes["POST"][path] = upload_handler


class HTTPHandler(BaseHTTPRequestHandler):
    """HTTP request handler with middleware and routing support."""

    # Disable Nagle's algorithm for lower latency
    disable_nagle_algorithm = True

    # Protocol version for keep-alive support
    protocol_version = "HTTP/1.1"

    # Increase buffer sizes for throughput
    rbufsize = 65536
    wbufsize = 65536

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass

    def _serve_html(self, filepath: str) -> bool:
        """Serve an HTML file. Returns True if file was served."""
        try:
            with open(filepath, "rb") as f:
                content = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return True
        except FileNotFoundError:
            return False

    def _respond(self, res: Response) -> None:
        # Fast path for JSON responses (most common)
        res_headers = res.headers
        content_type = res_headers.get("Content-Type")
        res_body = res.body

        if content_type is None:
            # Default: JSON response - most common path
            # orjson.dumps returns bytes directly, avoid double encode
            if _orjson is not None:
                body = _orjson.dumps(res_body)
            else:
                body = _json_dumps(res_body).encode()
            content_type = "application/json"
        elif content_type == "application/json":
            if _orjson is not None:
                body = _orjson.dumps(res_body)
            else:
                body = _json_dumps(res_body).encode()
        elif isinstance(res_body, bytes):
            body = res_body
        else:
            body = str(res_body).encode()

        # Use standard BaseHTTPRequestHandler methods (well-optimized)
        self.send_response(res.status)
        for k, v in res_headers.items():
            if k != "Content-Type":
                self.send_header(k, v)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle(self, method: str, body: Any = None) -> None:
        path, query = parse_query(self.path)
        req = Request(
            method=method,
            path=path,
            headers=dict(self.headers),
            query=query,
            body=body,
        )
        res = Response(status=200, body={}, headers={}, stop=False)

        # Run middleware - cache reference for speed
        middleware = app._middleware
        if middleware:
            for mw in middleware:
                mw(req, res)
                if res.stop:
                    self._respond(res)
                    return

        # Pre-hook - use pre-computed keys
        hook_key = _HTTP_HOOK_KEYS.get(method)
        ctx: HTTPContext | None = None  # Lazy initialization
        if hook_key:
            hooks = app._hooks.get(hook_key)
            if hooks:
                ctx = HTTPContext(req, res)
                for fn in hooks:
                    fn(ctx)
                    if res.stop:
                        self._respond(res)
                        return

        # Route lookup using radix tree router (supports :param and *wildcard)
        match = app._router.match_new(method, path)
        if match.matched and match.handler is not None:
            # Attach extracted parameters to request
            if match.params:
                req.params.update(match.params)

            # Call handler - supports both sync and async
            result = match.handler(req)
            # If result is a coroutine, run it synchronously
            if inspect.iscoroutine(result):
                result = _run_sync(result)

            if result:
                if isinstance(result, Response):
                    res.status = result.status
                    res.body = result.body
                    res.headers.update(result.headers)
                else:
                    # Use .get() with defaults to avoid multiple checks
                    res.status = result.get("status", res.status)
                    res.body = result.get("body", res.body)
                    headers = result.get("headers")
                    if headers:
                        res.headers.update(headers)
        elif not res.body:
            res.body = {"method": method, "path": path}

        # Post-hook - reuse ctx if already created
        if hook_key:
            after_hooks = app._hooks.get(hook_key + ":after")
            if after_hooks:
                if ctx is None:
                    ctx = HTTPContext(req, res)
                for fn in after_hooks:
                    fn(ctx)

        self._respond(res)

    def do_GET(self) -> None:
        # Serve index.html on root path
        if self.path == "/" or self.path == "/index.html":
            script_dir = os.path.dirname(os.path.abspath(__file__))
            if self._serve_html(os.path.join(script_dir, "index.html")):
                return
        self._handle("GET")

    def do_POST(self) -> None:
        content_length = self.headers.get("Content-Length")
        raw = self.rfile.read(int(content_length)) if content_length else b""
        content_type = self.headers.get("Content-Type", "")

        # Keep raw bytes for multipart uploads
        if "multipart/form-data" in content_type:
            body: Any = raw
        else:
            try:
                body = _json_loads(raw)
            except (json.JSONDecodeError, ValueError):
                body = raw.decode() if raw else ""

        self._handle("POST", body)


def handle_ws_connection(sock: socket.socket) -> None:
    """Handle a WebSocket connection lifecycle."""
    # Set socket options for better performance
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 32768)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 32768)

    ws = WSClient(sock)
    ws.handshake()

    app.emit("ws:connect", WSContext(ws))

    try:
        while True:
            opcode, data = ws.recv()

            if opcode is None or opcode == 8:
                ws.send(b"", 8)
                break

            if opcode == 9:
                ws.send(data or b"", 10)
                continue

            if opcode == 1 and data is not None:
                try:
                    msg = _json_loads(data)
                    ctx = WSMessage(ws=ws, data=msg, reply=None, stop=False)
                    app.emit("ws:message", ctx)

                    if ctx.stop:
                        continue
                    if ctx.reply is not None:
                        ws.send(ctx.reply)
                    else:
                        ws.send({"echo": msg})
                except (json.JSONDecodeError, ValueError):
                    ws.send({"error": "invalid json"})

    except (ConnectionResetError, BrokenPipeError, OSError):
        pass
    finally:
        app.emit("ws:disconnect", WSContext(ws))
        ws.close()


# ──────────────────────────────────────────────────────────────────────────────
# Async Server (aiohttp-based, optional)
# ──────────────────────────────────────────────────────────────────────────────


class AsyncServer:
    """Async HTTP + WebSocket server using aiohttp (if available).

    Provides significantly better performance than the threaded server
    by using a single event loop instead of thread-per-request.
    """

    @staticmethod
    async def _handle_request(request: Any) -> Any:
        """Handle HTTP request in async context."""
        method = request.method
        path_with_query = request.path_qs
        path, query = parse_query(path_with_query)

        # Parse body for POST requests
        body: Any = None
        if method == "POST":
            content_type = request.headers.get("Content-Type", "")
            if "multipart/form-data" in content_type:
                body = await request.read()
            else:
                raw = await request.read()
                try:
                    body = _json_loads(raw) if raw else None
                except (json.JSONDecodeError, ValueError):
                    body = raw.decode() if raw else ""

        # Create request/response context
        req = Request(
            method=method,
            path=path,
            headers=dict(request.headers),
            query=query,
            body=body,
        )
        res = Response(status=200, body={}, headers={}, stop=False)

        # Run middleware
        for mw in app._middleware:
            mw(req, res)
            if res.stop:
                break

        # Pre-hook
        hook_key = _HTTP_HOOK_KEYS.get(method)
        if not res.stop and hook_key:
            hooks = app._hooks.get(hook_key)
            if hooks:
                ctx = HTTPContext(req, res)
                for fn in hooks:
                    fn(ctx)
                    if res.stop:
                        break

        if not res.stop:
            # Route lookup using radix tree router (supports :param and *wildcard)
            match = app._router.match_new(method, path)
            if match.matched and match.handler is not None:
                # Attach extracted parameters to request
                if match.params:
                    req.params.update(match.params)

                result = match.handler(req)
                # Support async handlers natively
                if inspect.iscoroutine(result):
                    result = await result

                if result:
                    if isinstance(result, Response):
                        result = {
                            "status": result.status,
                            "body": result.body,
                            "headers": result.headers,
                        }
                    res.status = result.get("status", res.status)
                    res.body = result.get("body", res.body)
                    headers = result.get("headers")
                    if headers:
                        res.headers.update(headers)
            elif not res.body:
                res.body = {"method": method, "path": path}

            # Post-hook
            if hook_key:
                after_hooks = app._hooks.get(hook_key + ":after")
                if after_hooks:
                    ctx = HTTPContext(req, res)
                    for fn in after_hooks:
                        fn(ctx)

        # Build response
        content_type = res.headers.get("Content-Type")
        if content_type is None or content_type == "application/json":
            body_bytes = _json_dumps(res.body).encode()
            content_type = "application/json"
        elif isinstance(res.body, bytes):
            body_bytes = res.body
        else:
            body_bytes = str(res.body).encode()

        response_headers = {**res.headers, "Content-Type": content_type}
        return _aiohttp_web.Response(
            status=res.status,
            body=body_bytes,
            headers=response_headers,
        )

    @staticmethod
    async def _handle_websocket(
        request: Any,
    ) -> Any:
        """Handle WebSocket connection in async context."""
        ws_response = _aiohttp_web.WebSocketResponse()
        await ws_response.prepare(request)

        # Create a wrapper that mimics WSClient interface
        class AsyncWSClient:
            def __init__(self, ws: Any) -> None:
                self._ws = ws
                self.data = SimpleNamespace()

            def send(self, data: Any, opcode: int = 1) -> None:
                if opcode == 1 and not isinstance(data, (str, bytes)):
                    data = _json_dumps(data)
                # Schedule send in event loop
                asyncio.create_task(
                    self._ws.send_str(data)
                    if isinstance(data, str)
                    else self._ws.send_bytes(data)
                )

            def close(self) -> None:
                asyncio.create_task(self._ws.close())

        ws_client = AsyncWSClient(ws_response)
        app.emit("ws:connect", WSContext(ws_client))  # type: ignore[arg-type]

        try:
            async for msg in ws_response:
                if msg.type == _aiohttp_web.WSMsgType.TEXT:
                    try:
                        data = _json_loads(msg.data)
                        ctx = WSMessage(ws=ws_client, data=data, reply=None, stop=False)  # type: ignore[arg-type]
                        app.emit("ws:message", ctx)

                        if ctx.stop:
                            continue
                        if ctx.reply is not None:
                            ws_client.send(ctx.reply)
                        else:
                            ws_client.send({"echo": data})
                    except (json.JSONDecodeError, ValueError):
                        ws_client.send({"error": "invalid json"})
                elif msg.type == _aiohttp_web.WSMsgType.CLOSE:
                    break
        except Exception:
            pass
        finally:
            app.emit("ws:disconnect", WSContext(ws_client))  # type: ignore[arg-type]

        return ws_response

    @staticmethod
    def run(port: int = 8000, ws: int = 8765, host: str = "127.0.0.1") -> None:
        """Run the async server."""
        if _aiohttp_web is None:
            raise ImportError(
                "aiohttp is required for AsyncServer. Install with: pip install aiohttp"
            )

        async def start_server() -> None:
            aiohttp_app = _aiohttp_web.Application()

            # Add routes - catch all HTTP methods
            aiohttp_app.router.add_route("*", "/{path:.*}", AsyncServer._handle_request)

            # Add WebSocket route
            aiohttp_app.router.add_route("GET", "/ws", AsyncServer._handle_websocket)

            runner = _aiohttp_web.AppRunner(aiohttp_app)
            await runner.setup()

            site = _aiohttp_web.TCPSite(runner, host, port)
            await site.start()

            print(f"[HTTP+WS Async] -> http://{host}:{port}")
            print(f"[WebSocket] -> ws://{host}:{port}/ws")

            app.emit("server:start", ServerContext(None))  # type: ignore[arg-type]

            # Keep running
            try:
                while True:
                    await asyncio.sleep(3600)
            except asyncio.CancelledError:
                pass
            finally:
                app.emit("server:stop", ServerContext(None))  # type: ignore[arg-type]
                await runner.cleanup()

        with contextlib.suppress(KeyboardInterrupt):
            asyncio.run(start_server())


# ──────────────────────────────────────────────────────────────────────────────
# Server
# ──────────────────────────────────────────────────────────────────────────────


@dataclass
class Server:
    """HTTP + WebSocket server with clean start/stop lifecycle."""

    http_port: int = 8000
    ws_port: int = 8765
    websockets: bool = True
    host: str = "127.0.0.1"
    _http: ThreadingHTTPServer | None = field(default=None, repr=False)
    _ws: socket.socket | None = field(default=None, repr=False)
    _running: bool = field(default=False, repr=False)

    def _ws_loop(self) -> None:
        """Accept WebSocket connections."""
        while self._running and self._ws is not None:
            try:
                sock, _ = self._ws.accept()
                threading.Thread(
                    target=handle_ws_connection, args=(sock,), daemon=True
                ).start()
            except TimeoutError:
                pass

    def start(self) -> None:
        """Start both HTTP and WebSocket servers."""
        self._running = True
        app.emit("server:start", ServerContext(self))

        # Use ThreadingHTTPServer (faster than ThreadPool for this workload)
        self._http = ThreadingHTTPServer((self.host, self.http_port), HTTPHandler)
        threading.Thread(target=self._http.serve_forever, daemon=True).start()

        if self.websockets:  # start WebSocket server
            self._ws = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._ws.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._ws.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            # Increase socket buffer sizes for better throughput
            self._ws.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 65536)
            self._ws.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 65536)
            self._ws.bind((self.host, self.ws_port))
            self._ws.listen(512)  # Higher backlog for high load
            self._ws.settimeout(0.5)
            threading.Thread(target=self._ws_loop, daemon=True).start()
            print(f"[WebSocket] -> ws://localhost:{self.ws_port}")

        print(f"[HTTP] -> http://localhost:{self.http_port}")

    def stop(self) -> None:
        """Stop both servers."""
        self._running = False
        app.emit("server:stop", ServerContext(self))

        if self._http:
            self._http.shutdown()
        if self._ws:
            self._ws.close()

    @staticmethod
    def run(
        port: int = 8000,
        ws: int = 8765,
        host: str = "127.0.0.1",
        plugin: list[Plugin] | None = None,
        use_async: bool | None = None,
    ) -> None:
        """Lightweight (`HTTP` + `WebSocket`) server with `plugin` system.

        Args:
            port: HTTP port (default: 8000)
            ws: WebSocket port (default: 8765)
            host: Host to bind to (default: 127.0.0.1)
            plugin: List of plugins to register
            use_async: Force async mode (True), threaded mode (False),
                       or auto-detect (None, uses aiohttp if available)
        """
        if plugin:
            for p in plugin:
                app.plugin(p)

        # Determine which server to use
        should_use_async = (
            use_async if use_async is not None else (_ASYNC_BACKEND == "aiohttp")
        )

        if should_use_async and _aiohttp_web is not None:
            # Use async aiohttp server
            AsyncServer.run(port=port, ws=ws, host=host)
        else:
            # Fall back to threaded server
            server = Server(http_port=port, ws_port=ws, host=host)
            server.start()
            try:
                while server._running:
                    threading.Event().wait(0.5)
            except KeyboardInterrupt:
                pass
            finally:
                server.stop()
