"""Lightweight HTTP + WebSocket server with plugin system."""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import socket
import struct
import threading
from base64 import b64encode
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from enum import Enum
from hashlib import sha1
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any, NamedTuple, Protocol, TypedDict

from pykrozen.router import RadixRouter, RouteMatch

# ──────────────────────────────────────────────────────────────────────────────
# Enums and Constants
# ──────────────────────────────────────────────────────────────────────────────


class HTTPMethod(str, Enum):
    """HTTP method constants."""

    GET = "GET"
    POST = "POST"
    PUT = "PUT"
    DELETE = "DELETE"
    PATCH = "PATCH"


class WSOpcode(int, Enum):
    """WebSocket frame opcodes."""

    TEXT = 1
    BINARY = 2
    CLOSE = 8
    PING = 9
    PONG = 10


# Common Content-Type constants
CONTENT_TYPE_JSON = "application/json"
CONTENT_TYPE_HTML = "text/html; charset=utf-8"
CONTENT_TYPE_TEXT = "text/plain; charset=utf-8"
CONTENT_TYPE_OCTET = "application/octet-stream"

# ──────────────────────────────────────────────────────────────────────────────
# Settings
# ──────────────────────────────────────────────────────────────────────────────


@dataclass
class Settings:
    """Global server configuration. Modify before calling `Server.run()`."""

    port: int = 8000
    host: str = "127.0.0.1"
    ws_path: str = "/ws"
    debug: bool = False
    base_dir: Path = Path.cwd()


# Global settings instance
settings = Settings()


# ──────────────────────────────────────────────────────────────────────────────
# JSON and XOR helpers (pure stdlib)
# ──────────────────────────────────────────────────────────────────────────────


def _json_dumps(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"))


def _json_loads(data: bytes | str) -> Any:
    return json.loads(data)


def _xor_unmask_fast(payload: bytearray, mask: bytes) -> bytes:
    """Pure Python XOR unmask."""
    for i in range(len(payload)):
        payload[i] ^= mask[i & 3]
    return bytes(payload)


WS_MAGIC = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

# Pre-computed hook keys to avoid string operations per-request
_HTTP_HOOK_KEYS: dict[str, str] = {
    HTTPMethod.GET: "http:get",
    HTTPMethod.POST: "http:post",
    HTTPMethod.PUT: "http:put",
    HTTPMethod.DELETE: "http:delete",
    HTTPMethod.PATCH: "http:patch",
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


# ──────────────────────────────────────────────────────────────────────────────
# Type Definitions
# ──────────────────────────────────────────────────────────────────────────────


class Route(NamedTuple):
    """Route definition."""

    method: str
    path: str
    handler: Callable[..., Any]


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
        self.extra = kwargs  # Store any additional kwargs in extra

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


# ──────────────────────────────────────────────────────────────────────────────
# WebSocket Client
# ──────────────────────────────────────────────────────────────────────────────


@dataclass
class WSClient:
    """WebSocket client connection with send/recv/close methods."""

    sock: socket.socket
    data: SimpleNamespace = field(default_factory=SimpleNamespace)

    def _send_handshake_response(self, key: bytes) -> None:
        accept = b64encode(sha1(key + WS_MAGIC).digest())
        self.sock.send(
            b"HTTP/1.1 101 Switching Protocols\r\n"
            b"Upgrade: websocket\r\nConnection: Upgrade\r\n"
            b"Sec-WebSocket-Accept: " + accept + b"\r\n\r\n"
        )

    def handshake(self) -> None:
        """Perform WebSocket handshake."""
        req = self.sock.recv(1024)
        key = next(
            line.split(b": ", 1)[1]
            for line in req.split(b"\r\n")
            if line.lower().startswith(b"sec-websocket-key")
        )
        self._send_handshake_response(key)

    def handshake_with_key(self, key: str) -> None:
        """Perform WebSocket handshake with pre-parsed key."""
        self._send_handshake_response(key.encode())

    def recv(self) -> tuple[int | None, bytes | None]:
        """Receive a WebSocket frame, returns (opcode, data)."""
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

    def send(self, data: Any, opcode: int = WSOpcode.TEXT) -> None:
        """Send data over WebSocket (auto-serializes dicts to JSON)."""
        if opcode == WSOpcode.TEXT and not isinstance(data, (str, bytes)):
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
    """WebSocket message context: ws, data, reply, stop."""

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
    """Protocol for plugins. Implement `setup()` to register hooks/routes."""

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
    """Application Base Class with routing, middleware, and hooks."""

    _hooks: dict[str, list[HookHandler]] = field(default_factory=dict)
    _middleware: list[MiddlewareFn] = field(default_factory=list)
    _router: RadixRouter = field(default_factory=RadixRouter)
    # Legacy routes dict for backwards compatibility
    _routes: dict[str, dict[str, RouteHandler]] = field(
        default_factory=lambda: {
            HTTPMethod.GET: {},
            HTTPMethod.POST: {},
            HTTPMethod.PUT: {},
            HTTPMethod.DELETE: {},
            HTTPMethod.PATCH: {},
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

    def _register_route(
        self, method: HTTPMethod, path: str, fn: RouteHandler
    ) -> RouteHandler:
        """Register a route with both radix router and legacy dict."""
        self._router.add(method, path, fn)
        self._routes.setdefault(method, {})[path] = fn
        return fn

    def _make_route_decorator(
        self, method: HTTPMethod, path: str
    ) -> Callable[[RouteHandler], RouteHandler]:
        """Create a route decorator for the given HTTP method."""

        def decorator(fn: RouteHandler) -> RouteHandler:
            return self._register_route(method, path, fn)

        return decorator

    def get(self, path: str) -> Callable[[RouteHandler], RouteHandler]:
        """Decorator to register a GET route handler."""
        return self._make_route_decorator(HTTPMethod.GET, path)

    def post(self, path: str) -> Callable[[RouteHandler], RouteHandler]:
        """Decorator to register a POST route handler."""
        return self._make_route_decorator(HTTPMethod.POST, path)

    def put(self, path: str) -> Callable[[RouteHandler], RouteHandler]:
        """Decorator to register a PUT route handler."""
        return self._make_route_decorator(HTTPMethod.PUT, path)

    def delete(self, path: str) -> Callable[[RouteHandler], RouteHandler]:
        """Decorator to register a DELETE route handler."""
        return self._make_route_decorator(HTTPMethod.DELETE, path)

    def patch(self, path: str) -> Callable[[RouteHandler], RouteHandler]:
        """Decorator to register a PATCH route handler."""
        return self._make_route_decorator(HTTPMethod.PATCH, path)

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
        """Find and execute a route handler using radix tree matching."""
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

    def static(self, path_prefix: str):
        @get(f"{path_prefix}/*path")
        def static_base(req):
            path = req.params["path"]
            return file(settings.base_dir / "static" / path)

        return static_base


@dataclass
class Plugin:
    """Base plugin class. Subclass and override `setup()`."""

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
static = app.static


# ──────────────────────────────────────────────────────────────────────────────
# HTTP Handling
# ──────────────────────────────────────────────────────────────────────────────


# Empty dict singleton to avoid allocations
_EMPTY_DICT: dict[str, str] = {}


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
    ".html": CONTENT_TYPE_HTML,
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": f"{CONTENT_TYPE_JSON}; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".webp": "image/webp",
    ".pdf": "application/pdf",
    ".txt": CONTENT_TYPE_TEXT,
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
    """Create an HTML response."""
    return {
        "body": content,
        "headers": {"Content-Type": CONTENT_TYPE_HTML},
        "status": status,
    }


def text(content: str, status: int = 200) -> ResponseDict:
    """Create a plain text response."""
    return {
        "body": content,
        "headers": {"Content-Type": CONTENT_TYPE_TEXT},
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
        return error("File not found", 404)

    if content_type is None:
        ext = os.path.splitext(_filepath)[1].lower()
        content_type = MIME_TYPES.get(ext, CONTENT_TYPE_OCTET)

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
        content_type = headers.get("content-type", CONTENT_TYPE_OCTET)

        files.append(
            {
                "filename": filename,
                "content_type": content_type,
                "content": content,
            }
        )

    return files


def upload(path: str, handler: UploadHandler) -> None:
    """Register an upload endpoint for multipart file uploads."""

    def upload_handler(req: Request) -> ResponseDict:
        content_type = req.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            return error("Expected multipart/form-data")

        # Extract boundary
        if "boundary=" not in content_type:
            return error("Missing boundary")

        boundary = content_type.split("boundary=")[1].strip().encode()
        if not isinstance(req.body, bytes):
            return error("Invalid body")

        files = _parse_multipart(req.body, boundary)
        return handler(files)

    app._routes[HTTPMethod.POST][path] = upload_handler


def _handle_ws_loop(ws: WSClient) -> None:
    """Handle WebSocket message loop."""
    app.emit("ws:connect", WSContext(ws))

    try:
        while True:
            opcode, data = ws.recv()

            if opcode is None or opcode == WSOpcode.CLOSE:
                ws.send(b"", WSOpcode.CLOSE)
                break

            if opcode == WSOpcode.PING:
                ws.send(data or b"", WSOpcode.PONG)
                continue

            if opcode == WSOpcode.TEXT and data is not None:
                try:
                    # Decode bytes to string first if needed
                    text = data.decode("utf-8") if isinstance(data, bytes) else data
                    msg = _json_loads(text)
                    ctx = WSMessage(ws=ws, data=msg, reply=None, stop=False)
                    app.emit("ws:message", ctx)

                    if ctx.stop:
                        continue
                    if ctx.reply is not None:
                        ws.send(ctx.reply)
                    else:
                        ws.send({"echo": msg})
                except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
                    ws.send({"error": "invalid json"})

    except (ConnectionResetError, BrokenPipeError, OSError):
        pass
    finally:
        app.emit("ws:disconnect", WSContext(ws))
        ws.close()


def handle_ws_upgrade(sock: socket.socket, ws_key: str) -> None:
    """Handle WebSocket upgrade from HTTP."""
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 32768)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 32768)

    ws = WSClient(sock)
    ws.handshake_with_key(ws_key)
    _handle_ws_loop(ws)


class HTTPHandler(BaseHTTPRequestHandler):
    """HTTP request handler with middleware and routing."""

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
        """Serve an HTML file, returns True if served."""
        try:
            with open(filepath, "rb") as f:
                content = f.read()
            self.send_response(200)
            self.send_header("Content-Type", CONTENT_TYPE_HTML)
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
            body = _json_dumps(res_body).encode()
            content_type = CONTENT_TYPE_JSON
        elif content_type == CONTENT_TYPE_JSON:
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

    def _handle(self, method: HTTPMethod, body: Any = None) -> None:
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
        # Check for WebSocket upgrade on configured path
        path = self.path.split("?")[0]  # Remove query string
        if path == settings.ws_path:
            upgrade = self.headers.get("Upgrade", "").lower()
            if upgrade == "websocket":
                ws_key = self.headers.get("Sec-WebSocket-Key")
                if ws_key:
                    sock = self.connection
                    threading.Thread(
                        target=handle_ws_upgrade,
                        args=(sock, ws_key),
                        daemon=True,
                    ).start()
                    return

        # Serve index.html on root path
        if self.path in ("/", "/index.html"):
            script_dir = os.path.dirname(os.path.abspath(__file__))
            if self._serve_html(os.path.join(script_dir, "index.html")):
                return
        self._handle(HTTPMethod.GET)

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

        self._handle(HTTPMethod.POST, body)


# ──────────────────────────────────────────────────────────────────────────────
# Server
# ──────────────────────────────────────────────────────────────────────────────


@dataclass
class Server:
    """HTTP + WebSocket server with start/stop lifecycle."""

    _http: ThreadingHTTPServer | None = field(default=None, repr=False)
    _running: bool = field(default=False, repr=False)

    def start(self) -> None:
        """Start the server."""
        self._running = True
        app.emit("server:start", ServerContext(self))

        self._http = ThreadingHTTPServer((settings.host, settings.port), HTTPHandler)
        threading.Thread(target=self._http.serve_forever, daemon=True).start()

    def stop(self) -> None:
        """Stop the server."""
        self._running = False
        app.emit("server:stop", ServerContext(self))

        if self._http:
            self._http.shutdown()

    @staticmethod
    def run(plugin: list[Plugin] | None = None) -> None:
        """Start the server using global settings, optionally with plugins."""
        if plugin:
            for p in plugin:
                app.plugin(p)

        server = Server()
        server.start()
        # Before running server
        if settings.debug:
            print(f"[Server] http://{settings.host}:{settings.port}")
            print(f"[WebSocket] ws://{settings.host}:{settings.port}{settings.ws_path}")

        # Keep main thread alive
        try:
            while server._running:
                threading.Event().wait(0.5)
        except KeyboardInterrupt:
            pass
        finally:
            server.stop()


__version__ = "0.1.0"
__all__ = [
    # Version
    "__version__",
    # Enums and Constants
    "HTTPMethod",
    "WSOpcode",
    "CONTENT_TYPE_JSON",
    "CONTENT_TYPE_HTML",
    "CONTENT_TYPE_TEXT",
    "CONTENT_TYPE_OCTET",
    # Core classes
    "Server",
    "Settings",
    "settings",
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
    # Global functions
    "static",
    # Factory functions
    "make_request",
    "make_response",
    # Response helpers
    "html",
    "text",
    "error",
    "file",
    "upload",
    # Type aliases
    "MiddlewareFn",
    "RouteHandler",
    "HookHandler",
    "UploadHandler",
    "PluginProtocol",
    # Router
    "RadixRouter",
    "RouteMatch",
]
