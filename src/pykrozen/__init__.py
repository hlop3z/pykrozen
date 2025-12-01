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
    "App",
    "Plugin",
    # Request/Response
    "Request",
    "Response",
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
    "on",
    "use",
    # Factory functions
    "make_request",
    "make_response",
    # Type aliases
    "MiddlewareFn",
    "RouteHandler",
    "HookHandler",
    "PluginProtocol",
]

import json
import os
import socket
import struct
import threading
from base64 import b64encode
from collections.abc import Callable
from dataclasses import dataclass, field
from hashlib import sha1
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from typing import Any, NamedTuple, Protocol

WS_MAGIC = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


# ──────────────────────────────────────────────────────────────────────────────
# Type Definitions
# ──────────────────────────────────────────────────────────────────────────────


class Route(NamedTuple):
    """Route definition."""

    method: str
    path: str
    handler: Callable[..., Any]


class Request(SimpleNamespace):
    """HTTP request context. Extend by adding attributes."""

    method: str
    path: str
    headers: dict[str, str]
    query: dict[str, str]
    body: Any


class Response(SimpleNamespace):
    """HTTP response context. Set stop=True to halt middleware."""

    status: int
    body: Any
    headers: dict[str, str]
    stop: bool


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
            payload = self.sock.recv(length)
            data = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        else:
            data = self.sock.recv(length)

        return opcode, data

    def send(self, data: Any, opcode: int = 1) -> None:
        """Send data over WebSocket. Auto-serializes dicts to JSON."""
        if opcode == 1 and not isinstance(data, (str, bytes)):
            data = json.dumps(data)
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


class WSMessage(SimpleNamespace):
    """WebSocket message context."""

    ws: WSClient
    data: Any
    reply: Any
    stop: bool


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

MiddlewareFn = Callable[[Request, Response], None]
RouteHandler = Callable[[Request], Response | dict[str, Any] | None]
HookHandler = Callable[[Any], None]


# ──────────────────────────────────────────────────────────────────────────────
# App
# ──────────────────────────────────────────────────────────────────────────────


@dataclass
class App:
    """Central registry for plugins, hooks, routes, and middleware."""

    _hooks: dict[str, list[HookHandler]] = field(default_factory=dict)
    _middleware: list[MiddlewareFn] = field(default_factory=list)
    _routes: dict[str, dict[str, RouteHandler]] = field(
        default_factory=lambda: {"GET": {}, "POST": {}}
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

    def get(self, path: str) -> Callable[[RouteHandler], RouteHandler]:
        """Decorator to register a GET route handler."""

        def decorator(fn: RouteHandler) -> RouteHandler:
            self._routes["GET"][path] = fn
            return fn

        return decorator

    def post(self, path: str) -> Callable[[RouteHandler], RouteHandler]:
        """Decorator to register a POST route handler."""

        def decorator(fn: RouteHandler) -> RouteHandler:
            self._routes["POST"][path] = fn
            return fn

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

    def route(self, method: str, path: str, req: Request) -> dict[str, Any] | None:
        """Find and execute a route handler."""
        handler = self._routes.get(method, {}).get(path)
        if not handler:
            return None
        result = handler(req)
        if isinstance(result, SimpleNamespace):
            return vars(result)
        return result


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


# ──────────────────────────────────────────────────────────────────────────────
# HTTP Handling
# ──────────────────────────────────────────────────────────────────────────────


def parse_query(path: str) -> tuple[str, dict[str, str]]:
    """Parse query string from path."""
    if "?" not in path:
        return path, {}
    base, qs = path.split("?", 1)
    params = {}
    for pair in qs.split("&"):
        if "=" in pair:
            k, v = pair.split("=", 1)
            params[k] = v
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
    return Response(status=status, body=body or {}, headers=headers or {}, stop=False)


class HTTPHandler(BaseHTTPRequestHandler):
    """HTTP request handler with middleware and routing support."""

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
        body = json.dumps(res.body).encode()
        self.send_response(res.status)
        for k, v in res.headers.items():
            self.send_header(k, v)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle(self, method: str, body: Any = None) -> None:
        path, query = parse_query(self.path)
        req = make_request(method, path, dict(self.headers), query, body)
        res = make_response()

        if not app.run_middleware(req, res):
            self._respond(res)
            return

        ctx = HTTPContext(req, res)
        app.emit(f"http:{method.lower()}", ctx)
        if res.stop:
            self._respond(res)
            return

        routed = app.route(method, path, req)
        if routed:
            if "status" in routed:
                res.status = routed["status"]
            if "body" in routed:
                res.body = routed["body"]
            if "headers" in routed:
                res.headers.update(routed["headers"])
        elif not res.body:
            res.body = {"method": method, "path": path}

        app.emit(f"http:{method.lower()}:after", ctx)
        self._respond(res)

    def do_GET(self) -> None:
        # Serve index.html on root path
        if self.path == "/" or self.path == "/index.html":
            script_dir = os.path.dirname(os.path.abspath(__file__))
            if self._serve_html(os.path.join(script_dir, "index.html")):
                return
        self._handle("GET")

    def do_POST(self) -> None:
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        try:
            body = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            body = raw.decode()
        self._handle("POST", body)


def handle_ws_connection(sock: socket.socket) -> None:
    """Handle a WebSocket connection lifecycle."""
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
                    msg = json.loads(data)
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

        self._http = ThreadingHTTPServer((self.host, self.http_port), HTTPHandler)
        threading.Thread(target=self._http.serve_forever, daemon=True).start()

        if self.websockets:  # start WebSocket server
            self._ws = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._ws.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._ws.bind((self.host, self.ws_port))
            self._ws.listen()
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
    ) -> None:
        """Lightweight (`HTTP` + `WebSocket`) server with `plugin` system."""
        server = Server(http_port=port, ws_port=ws, host=host)
        if plugin:
            for p in plugin:
                app.plugin(p)
        server.start()
        try:
            while server._running:
                threading.Event().wait(0.5)
        except KeyboardInterrupt:
            pass
        finally:
            server.stop()
