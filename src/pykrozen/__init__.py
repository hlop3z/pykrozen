"""Lightweight HTTP + WebSocket server with plugin system."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import multiprocessing
import os
import socket
import struct
import sys
import threading
from base64 import b64encode
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from enum import Enum
from hashlib import sha1
from pathlib import Path
from types import SimpleNamespace
from typing import Any, NamedTuple, Protocol, TypedDict, cast

from pykrozen.router import RadixRouter, RouteMatch

# Platform-specific socket option (Unix only)
SO_REUSEPORT: int | None = getattr(socket, "SO_REUSEPORT", None)

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
    """Global server configuration. Modify before calling `Server.run()`.

    Basic Settings:
        port: HTTP server port (default: 8000)
        host: Bind address (default: 127.0.0.1)
        ws_path: WebSocket upgrade path (default: /ws)
        debug: Enable debug output (default: False)
        base_dir: Base directory for static files (default: cwd)

    Performance Settings:
        workers: Number of worker processes. Set >1 for multi-process mode
                 on Unix/Linux. Uses SO_REUSEPORT for kernel load balancing.
                 (default: 1, single-process mode)
        backlog: Socket listen backlog for pending connections (default: 2048)
        reuse_port: Enable SO_REUSEPORT socket option (default: True)

    Example:
        >>> from pykrozen import settings, Server
        >>> settings.port = 3000
        >>> settings.workers = 4  # Unix only
        >>> Server.run()
    """

    port: int = 8000
    host: str = "127.0.0.1"
    ws_path: str = "/ws"
    debug: bool = False
    base_dir: Path = Path.cwd()
    # Performance settings
    workers: int = 1  # Number of worker processes (1 = single process)
    backlog: int = 2048  # Socket listen backlog
    reuse_port: bool = True  # Enable SO_REUSEPORT for load balancing


# Global settings instance
settings = Settings()


# ──────────────────────────────────────────────────────────────────────────────
# JSON and XOR helpers (pure stdlib)
# ──────────────────────────────────────────────────────────────────────────────


def _json_dumps(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"))


# Pre-allocated byte arrays for common operations (buffer pooling pattern from Rust)
_BUFFER_POOL_SIZE = 32
_buffer_pool: list[bytearray] = [bytearray(4096) for _ in range(_BUFFER_POOL_SIZE)]
_buffer_pool_lock = threading.Lock()
_buffer_pool_index = 0


def _get_pooled_buffer(size: int = 4096) -> bytearray:
    """Get a buffer from the pool or create new one (Rust buffer pooling pattern)."""
    global _buffer_pool_index
    if size <= 4096:
        with _buffer_pool_lock:
            buf = _buffer_pool[_buffer_pool_index]
            _buffer_pool_index = (_buffer_pool_index + 1) % _BUFFER_POOL_SIZE
            return buf
    return bytearray(size)


# Pre-encoded common responses to avoid repeated serialization
_CACHED_RESPONSES: dict[str, bytes] = {}
_CACHE_MAX_SIZE = 2000
_CACHE_MAX_ITEM_SIZE = 512


def _json_dumps_cached(obj: Any) -> bytes:
    """JSON encode with caching for common small objects (LRU-style)."""
    # Only cache small, hashable objects
    if isinstance(obj, dict) and len(obj) <= 5:
        try:
            # Create a hashable key from the dict
            key = str(sorted(obj.items()))
            cached = _CACHED_RESPONSES.get(key)
            if cached is not None:
                return cached
            result = json.dumps(obj, separators=(",", ":")).encode()
            # Only cache if small enough
            if len(result) < _CACHE_MAX_ITEM_SIZE:
                # Simple cache eviction: clear half when full
                if len(_CACHED_RESPONSES) >= _CACHE_MAX_SIZE:
                    # Keep newest half (Python 3.7+ dicts maintain insertion order)
                    keys = list(_CACHED_RESPONSES.keys())
                    for k in keys[: len(keys) // 2]:
                        del _CACHED_RESPONSES[k]
                _CACHED_RESPONSES[key] = result
            return result
        except (TypeError, ValueError):
            pass
    return json.dumps(obj, separators=(",", ":")).encode()


def _json_loads(data: bytes | str) -> Any:
    return json.loads(data)


def _xor_unmask_fast(payload: bytearray, mask: bytes) -> bytes:
    """Optimized XOR unmask using 64-bit operations where possible."""
    length = len(payload)
    if length == 0:
        return b""

    # Build 8-byte mask by repeating 4-byte mask twice
    mask8 = mask * 2
    mask_int = int.from_bytes(mask8, "little")

    # Process 8 bytes at a time
    i = 0
    chunks = length // 8
    for _ in range(chunks):
        chunk_int = int.from_bytes(payload[i : i + 8], "little")
        result_int = chunk_int ^ mask_int
        payload[i : i + 8] = result_int.to_bytes(8, "little")
        i += 8

    # Handle remaining bytes
    for j in range(i, length):
        payload[j] ^= mask[j & 3]

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
    _send_lock: threading.Lock = field(default_factory=threading.Lock)
    _recv_lock: threading.Lock = field(default_factory=threading.Lock)

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

    def _recv_exact(self, n: int) -> bytes:
        """Read exactly n bytes from socket, handling partial reads."""
        buf = bytearray()
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("Connection closed")
            buf.extend(chunk)
        return bytes(buf)

    def recv(self) -> tuple[int | None, bytes | None]:
        """Receive a WebSocket frame, returns (opcode, data)."""
        with self._recv_lock:
            try:
                header = self._recv_exact(2)

                opcode = header[0] & 0x0F
                length = header[1] & 0x7F

                if length == 126:
                    length = struct.unpack(">H", self._recv_exact(2))[0]
                elif length == 127:
                    length = struct.unpack(">Q", self._recv_exact(8))[0]

                if header[1] & 0x80:
                    mask = self._recv_exact(4)
                    # Read payload using exact reads to handle TCP fragmentation
                    payload = self._recv_exact(length) if length > 0 else b""
                    # XOR unmask
                    data = _xor_unmask_fast(bytearray(payload), mask)
                else:
                    data = self._recv_exact(length) if length > 0 else b""

                return opcode, data
            except (ConnectionError, OSError, struct.error):
                return None, None

    def send(self, data: Any, opcode: int = WSOpcode.TEXT) -> bool:
        """Send data over WebSocket (auto-serializes dicts to JSON)."""
        with self._send_lock:
            try:
                if opcode == WSOpcode.TEXT and not isinstance(data, (str, bytes)):
                    data = _json_dumps(data)
                payload = data.encode() if isinstance(data, str) else data
                length = len(payload)

                # Fast path for small payloads (most common case)
                if length < 126:
                    # bytes() + concat is faster than bytearray for small frames
                    self.sock.sendall(bytes([0x80 | opcode, length]) + payload)
                elif length < 0x10000:
                    header = bytes([0x80 | opcode, 126]) + struct.pack(">H", length)
                    self.sock.sendall(header + payload)
                else:
                    header = bytes([0x80 | opcode, 127]) + struct.pack(">Q", length)
                    self.sock.sendall(header + payload)
                return True
            except (OSError, BrokenPipeError, ConnectionError):
                return False

    def close(self) -> None:
        """Close the connection."""
        self.sock.close()


class AsyncWSClient:
    """Async WebSocket client that works with asyncio transports."""

    __slots__ = ("transport", "data", "_buffer", "_send_lock", "_data_event", "_closed")

    def __init__(self, transport: asyncio.Transport) -> None:
        self.transport = transport
        self.data: SimpleNamespace = SimpleNamespace()
        self._buffer = bytearray()
        self._send_lock = asyncio.Lock()
        self._data_event: asyncio.Event = asyncio.Event()
        self._closed = False

    async def _wait_for_bytes(self, n: int) -> bool:
        """Wait for at least n bytes in buffer using event-driven approach."""
        while len(self._buffer) < n:
            if self._closed or not self.transport or self.transport.is_closing():
                return False
            self._data_event.clear()
            # Use wait_for with short timeout to check connection state
            try:
                await asyncio.wait_for(self._data_event.wait(), timeout=0.1)
            except TimeoutError:
                continue
        return True

    async def recv_async(self) -> tuple[int | None, bytes | None]:
        """Receive a WebSocket frame asynchronously (event-driven, no polling)."""
        try:
            # Wait for header (2 bytes)
            if not await self._wait_for_bytes(2):
                return None, None

            header = bytes(self._buffer[:2])
            opcode = header[0] & 0x0F
            length = header[1] & 0x7F
            offset = 2

            # Extended length
            if length == 126:
                if not await self._wait_for_bytes(offset + 2):
                    return None, None
                length = struct.unpack(">H", bytes(self._buffer[offset : offset + 2]))[
                    0
                ]
                offset += 2
            elif length == 127:
                if not await self._wait_for_bytes(offset + 8):
                    return None, None
                length = struct.unpack(">Q", bytes(self._buffer[offset : offset + 8]))[
                    0
                ]
                offset += 8

            # Mask
            masked = header[1] & 0x80
            mask = b""
            if masked:
                if not await self._wait_for_bytes(offset + 4):
                    return None, None
                mask = bytes(self._buffer[offset : offset + 4])
                offset += 4

            # Payload
            if not await self._wait_for_bytes(offset + length):
                return None, None

            payload = bytes(self._buffer[offset : offset + length])
            del self._buffer[: offset + length]

            # Unmask if needed
            if masked and mask:
                payload = _xor_unmask_fast(bytearray(payload), mask)

            return opcode, payload
        except Exception:
            return None, None

    def feed_data(self, data: bytes) -> None:
        """Feed received data into buffer and signal waiting coroutines."""
        self._buffer.extend(data)
        self._data_event.set()

    def mark_closed(self) -> None:
        """Mark connection as closed."""
        self._closed = True
        self._data_event.set()  # Wake up any waiting recv

    def send(self, data: Any, opcode: int = WSOpcode.TEXT) -> bool:
        """Send data over WebSocket."""
        try:
            if not self.transport or self.transport.is_closing():
                return False

            if opcode == WSOpcode.TEXT and not isinstance(data, (str, bytes)):
                data = _json_dumps(data)
            payload = data.encode() if isinstance(data, str) else data
            length = len(payload)

            if length < 126:
                self.transport.write(bytes([0x80 | opcode, length]) + payload)
            elif length < 0x10000:
                header = bytes([0x80 | opcode, 126]) + struct.pack(">H", length)
                self.transport.write(header + payload)
            else:
                header = bytes([0x80 | opcode, 127]) + struct.pack(">Q", length)
                self.transport.write(header + payload)
            return True
        except (OSError, BrokenPipeError, ConnectionError):
            return False

    def close(self) -> None:
        """Close the connection."""
        if self.transport and not self.transport.is_closing():
            self.transport.close()


class WSMessage:
    """WebSocket message context: ws, data, reply, stop."""

    __slots__ = ("ws", "data", "reply", "stop")

    ws: WSClient | AsyncWSClient
    data: Any
    reply: Any
    stop: bool

    def __init__(
        self,
        ws: WSClient | AsyncWSClient,
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

    ws: WSClient | AsyncWSClient


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

    def static(self, path_prefix: str) -> RouteHandler:
        @get(f"{path_prefix}/*path")
        def static_base(req: Request) -> ResponseDict:
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


# ──────────────────────────────────────────────────────────────────────────────
# Event Loop Selection (uvloop/winloop if available)
# ──────────────────────────────────────────────────────────────────────────────

_LOOP_POLICY: str = "stdlib"


def _setup_event_loop() -> None:
    """Set up the best available event loop policy."""
    global _LOOP_POLICY

    # Try uvloop first (Unix) - 2-4x faster than stdlib
    try:
        import uvloop

        asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
        _LOOP_POLICY = "uvloop"
        return
    except ImportError:
        pass

    # Try winloop (Windows) - uvloop port for Windows
    try:
        import winloop

        asyncio.set_event_loop_policy(winloop.EventLoopPolicy())
        _LOOP_POLICY = "winloop"
        return
    except ImportError:
        pass

    # Fall back to stdlib asyncio
    _LOOP_POLICY = "stdlib"


# ──────────────────────────────────────────────────────────────────────────────
# Async HTTP Protocol (high-performance asyncio-based server)
# ──────────────────────────────────────────────────────────────────────────────

# HTTP status messages
_HTTP_STATUS: dict[int, str] = {
    200: "OK",
    201: "Created",
    204: "No Content",
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
    500: "Internal Server Error",
}

# Pre-built response headers (Actix/Hyper pattern - avoid string formatting per request)
_PREBUILT_HEADERS: dict[int, bytes] = {
    status: f"HTTP/1.1 {status} {text}\r\n".encode()
    for status, text in _HTTP_STATUS.items()
}

# Pre-built common header lines
_HEADER_JSON = b"Content-Type: application/json\r\n"
_HEADER_KEEPALIVE = b"Connection: keep-alive\r\n"
_HEADER_CLOSE = b"Connection: close\r\n"


class AsyncHTTPProtocol(asyncio.Protocol):
    """High-performance asyncio HTTP protocol handler."""

    def __init__(self) -> None:
        self.transport: asyncio.Transport | None = None
        self.buffer: bytearray = bytearray()
        self.keep_alive: bool = True
        self.request_count: int = 0
        self._ws_client: AsyncWSClient | None = None
        self._ws_mode: bool = False

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = cast(asyncio.Transport, transport)
        # Socket optimizations (Gunicorn/uWSGI patterns)
        sock = transport.get_extra_info("socket")
        if sock:
            # Disable Nagle's algorithm for lower latency
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            # Increase socket buffers for throughput (64KB is optimal balance)
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 65536)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 65536)
            except OSError:
                pass  # Not all platforms support buffer resizing

    def connection_lost(self, exc: Exception | None) -> None:
        self.transport = None

    async def _ws_message_loop(self, ws: AsyncWSClient) -> None:
        """Handle WebSocket message loop asynchronously."""
        while True:
            opcode, data = await ws.recv_async()

            if opcode is None or opcode == WSOpcode.CLOSE:
                ws.send(b"", WSOpcode.CLOSE)
                break

            if opcode == WSOpcode.PING:
                ws.send(data or b"", WSOpcode.PONG)
                continue

            if opcode == WSOpcode.TEXT and data is not None:
                try:
                    text_data = (
                        data.decode("utf-8") if isinstance(data, bytes) else data
                    )
                    msg = _json_loads(text_data)
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

    async def _handle_websocket(self, ws_key: str, headers: dict[str, str]) -> None:
        """Handle WebSocket upgrade asynchronously using asyncio streams."""
        if not self.transport:
            return

        # Send upgrade response
        accept = b64encode(sha1(ws_key.encode() + WS_MAGIC).digest()).decode()
        response = (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
        )
        self.transport.write(response.encode())

        # Create async WebSocket client wrapper and switch to WS mode
        ws = AsyncWSClient(self.transport)
        ws.data = SimpleNamespace()
        self._ws_client = ws
        self._ws_mode = True

        # Emit connect event
        app.emit("ws:connect", WSContext(ws))

        try:
            await self._ws_message_loop(ws)
        except (ConnectionResetError, BrokenPipeError, OSError, asyncio.CancelledError):
            pass
        finally:
            ws.mark_closed()
            self._ws_mode = False
            self._ws_client = None
            app.emit("ws:disconnect", WSContext(ws))
            if self.transport and not self.transport.is_closing():
                self.transport.close()

    def _send_response(self, res: Response) -> None:
        """Send HTTP response using pre-built headers for speed."""
        if not self.transport:
            return

        # Serialize body
        content_type = res.headers.get("Content-Type")
        is_json = content_type is None or content_type == CONTENT_TYPE_JSON
        if is_json:
            body = _json_dumps_cached(res.body)
            content_type = CONTENT_TYPE_JSON
        elif isinstance(res.body, bytes):
            body = res.body
        else:
            body = str(res.body).encode()

        # Fast path: use pre-built status line if available
        status_line = _PREBUILT_HEADERS.get(res.status)
        if status_line is None:
            status_text = _HTTP_STATUS.get(res.status, "OK")
            status_line = f"HTTP/1.1 {res.status} {status_text}\r\n".encode()

        # Build response using bytes concatenation (faster than string join)
        parts = [status_line]

        # Content-Type header
        if is_json:
            parts.append(_HEADER_JSON)
        else:
            parts.append(f"Content-Type: {content_type}\r\n".encode())

        # Content-Length
        parts.append(f"Content-Length: {len(body)}\r\n".encode())

        # Custom headers
        for k, v in res.headers.items():
            if k != "Content-Type":
                parts.append(f"{k}: {v}\r\n".encode())

        # Connection header
        parts.append(_HEADER_KEEPALIVE if self.keep_alive else _HEADER_CLOSE)

        # End headers and body
        parts.append(b"\r\n")
        parts.append(body)

        # Single write call (more efficient than multiple writes)
        self.transport.write(b"".join(parts))

    async def _handle_request(
        self, method: str, path: str, headers: dict[str, str], body_data: bytes
    ) -> None:
        """Handle HTTP request asynchronously."""
        if not self.transport:
            return

        # Parse path and query
        path_clean, query = parse_query(path)

        # Parse body
        content_type = headers.get("Content-Type", "")
        if "multipart/form-data" in content_type:
            body: Any = body_data
        elif body_data:
            try:
                body = _json_loads(body_data)
            except (json.JSONDecodeError, ValueError):
                body = body_data.decode("utf-8", errors="replace")
        else:
            body = None

        # Create request/response
        req = Request(
            method=method, path=path_clean, headers=headers, query=query, body=body
        )
        res = Response(status=200, body={}, headers={}, stop=False)

        # Run middleware
        for mw in app._middleware:
            mw(req, res)
            if res.stop:
                self._send_response(res)
                return

        # Pre-hook
        hook_key = _HTTP_HOOK_KEYS.get(method)
        if hook_key:
            hooks = app._hooks.get(hook_key)
            if hooks:
                ctx = HTTPContext(req, res)
                for fn in hooks:
                    fn(ctx)
                    if res.stop:
                        self._send_response(res)
                        return

        # Route matching
        match = app._router.match_new(method, path_clean)
        if match.matched and match.handler is not None:
            if match.params:
                req.params.update(match.params)

            # Call handler (support async)
            result = match.handler(req)
            if inspect.iscoroutine(result):
                result = await result

            if result:
                if isinstance(result, Response):
                    res.status = result.status
                    res.body = result.body
                    res.headers.update(result.headers)
                else:
                    res.status = result.get("status", res.status)
                    res.body = result.get("body", res.body)
                    h = result.get("headers")
                    if h:
                        res.headers.update(h)
        elif not res.body:
            res.body = {"method": method, "path": path_clean}

        # Post-hook
        if hook_key:
            after_hooks = app._hooks.get(hook_key + ":after")
            if after_hooks:
                ctx = HTTPContext(req, res)
                for fn in after_hooks:
                    fn(ctx)

        self._send_response(res)

    def _send_error(self, status: int) -> None:
        """Send error response."""
        if self.transport:
            status_text = _HTTP_STATUS.get(status, "Error")
            body = f'{{"error":"{status_text}"}}'
            response = (
                f"HTTP/1.1 {status} {status_text}\r\n"
                f"Content-Type: {CONTENT_TYPE_JSON}\r\n"
                f"Content-Length: {len(body)}\r\n"
                "Connection: close\r\n\r\n"
                f"{body}"
            )
            self.transport.write(response.encode())
            self.transport.close()

    def _process_buffer(self) -> None:
        """Process buffered data for complete HTTP requests."""
        while b"\r\n\r\n" in self.buffer:
            # Find end of headers
            header_end = self.buffer.find(b"\r\n\r\n")
            if header_end == -1:
                return

            header_data = bytes(self.buffer[:header_end])
            body_start = header_end + 4

            # Parse request line and headers
            lines = header_data.split(b"\r\n")
            if not lines:
                self._send_error(400)
                return

            # Parse request line: METHOD PATH HTTP/1.x
            request_line = lines[0].decode("latin-1")
            parts = request_line.split(" ", 2)
            if len(parts) != 3:
                self._send_error(400)
                return

            method, path, _ = parts

            # Parse headers
            headers: dict[str, str] = {}
            for line in lines[1:]:
                if b": " in line:
                    key, val = line.split(b": ", 1)
                    headers[key.decode("latin-1")] = val.decode("latin-1")

            # Check for WebSocket upgrade
            if (
                path == settings.ws_path
                and headers.get("Upgrade", "").lower() == "websocket"
            ):
                ws_key = headers.get("Sec-WebSocket-Key")
                if ws_key and self.transport:
                    # Remove processed data
                    del self.buffer[:body_start]
                    # Handle WebSocket upgrade
                    asyncio.create_task(self._handle_websocket(ws_key, headers))
                    return

            # Determine content length
            content_length = int(headers.get("Content-Length", "0"))

            # Check if we have complete body
            if len(self.buffer) < body_start + content_length:
                return  # Wait for more data

            # Extract body
            body_data = bytes(self.buffer[body_start : body_start + content_length])
            del self.buffer[: body_start + content_length]

            # Process request
            self.request_count += 1
            asyncio.create_task(self._handle_request(method, path, headers, body_data))

    def data_received(self, data: bytes) -> None:
        # If in WebSocket mode, feed data to WS client
        if self._ws_mode and self._ws_client:
            self._ws_client.feed_data(data)
            return
        self.buffer.extend(data)
        self._process_buffer()


async def _run_async_server(host: str, port: int) -> None:
    """Run the async HTTP server."""
    loop = asyncio.get_event_loop()

    # Create server with optimized settings
    server = await loop.create_server(
        AsyncHTTPProtocol,
        host,
        port,
        reuse_address=True,
        reuse_port=SO_REUSEPORT is not None and settings.reuse_port,
        backlog=settings.backlog,
    )

    if settings.debug:
        loop_name = _LOOP_POLICY
        print(f"[AsyncServer] http://{host}:{port} (event loop: {loop_name})")
        print(f"[WebSocket] ws://{host}:{port}{settings.ws_path}")

    async with server:
        await server.serve_forever()


def _async_worker_process(host: str, port: int, worker_id: int) -> None:
    """Worker process running async server."""
    _setup_event_loop()
    if settings.debug:
        print(f"[Worker {worker_id}] Starting async server on {host}:{port}")
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_run_async_server(host, port))


# ──────────────────────────────────────────────────────────────────────────────
# Server
# ──────────────────────────────────────────────────────────────────────────────


@dataclass
class Server:
    """HTTP + WebSocket server with start/stop lifecycle and multiprocessing.

    Uses asyncio for high performance. Optionally uses uvloop (Unix) or
    winloop (Windows) if installed for even better performance.
    """

    _running: bool = field(default=False, repr=False)
    _workers: list[multiprocessing.Process] = field(default_factory=list, repr=False)

    def start(self) -> None:
        """Start the server with optional worker processes."""
        self._running = True
        app.emit("server:start", ServerContext(self))

        num_workers = settings.workers

        if num_workers > 1 and sys.platform != "win32":
            # Multiprocess mode (Unix only)
            for i in range(num_workers):
                p = multiprocessing.Process(
                    target=_async_worker_process,
                    args=(settings.host, settings.port, i),
                    daemon=True,
                )
                p.start()
                self._workers.append(p)
        else:
            # Single process mode
            _setup_event_loop()

            def run_async() -> None:
                with contextlib.suppress(KeyboardInterrupt):
                    asyncio.run(_run_async_server(settings.host, settings.port))

            threading.Thread(target=run_async, daemon=True).start()

    def stop(self) -> None:
        """Stop the server and all workers."""
        self._running = False
        app.emit("server:stop", ServerContext(self))

        # Stop worker processes
        for p in self._workers:
            p.terminate()
            p.join(timeout=1)
        self._workers.clear()

    @staticmethod
    def run(plugin: list[Plugin] | None = None) -> None:
        """Start the server using global settings, optionally with plugins."""
        if plugin:
            for p in plugin:
                app.plugin(p)

        server = Server()
        server.start()

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
