"""Lightweight HTTP + WebSocket server with plugin system."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import multiprocessing
import socket
import sys
import threading
from base64 import b64encode
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from hashlib import sha1
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Protocol

from pykrozen.constants import (
    CONTENT_TYPE_HTML,
    CONTENT_TYPE_JSON,
    CONTENT_TYPE_MULTIPART,
    CONTENT_TYPE_OCTET,
    CONTENT_TYPE_TEXT,
    ENCODING_UTF8,
    HEADER_CLOSE_BYTES,
    HEADER_CONTENT_TYPE,
    HEADER_JSON_BYTES,
    HEADER_KEEPALIVE_BYTES,
    HOOK_AFTER_SUFFIX,
    HTTP_STATUS,
    PREBUILT_STATUS_HEADERS,
    SERVER_MAIN_THREAD_WAIT,
    SOCKET_BUFFER_ASYNC,
    SOCKET_BUFFER_SYNC,
    WS_DEFAULT_ECHO_KEY,
    WS_ERROR_INVALID_JSON,
    WS_MAGIC,
    Hook,
    HTTPMethod,
    WSOpcode,
)
from pykrozen.files import clear_cache as clear_file_cache
from pykrozen.files import file
from pykrozen.http import (
    HTTPContext,
    Request,
    Response,
    ResponseHTTP,
    Route,
    RouteInfo,
    Router,
    UploadedFile,
    create_upload_handler,
    error,
    html,
    make_request,
    make_response,
    parse_query,
    router,
    text,
)
from pykrozen.radix import RadixRouter, RouteMatch
from pykrozen.utils import _json_dumps_cached, _json_loads
from pykrozen.ws import AsyncWSClient, WSClient, WSContext, WSMessage

__version__ = "0.1.0"

SO_REUSEPORT: int | None = getattr(socket, "SO_REUSEPORT", None)

_HTTP_HOOK_KEYS: dict[str, str] = {
    HTTPMethod.GET: Hook.HTTP_GET,
    HTTPMethod.POST: Hook.HTTP_POST,
    HTTPMethod.PUT: Hook.HTTP_PUT,
    HTTPMethod.DELETE: Hook.HTTP_DELETE,
    HTTPMethod.PATCH: Hook.HTTP_PATCH,
}


# ──────────────────────────────────────────────────────────────────────────────
# Settings
# ──────────────────────────────────────────────────────────────────────────────


@dataclass
class Settings:
    """Server configuration."""

    port: int = 8000
    host: str = "127.0.0.1"
    ws_path: str = "/ws"
    debug: bool = False
    base_dir: Path = field(default_factory=Path.cwd)
    workers: int = 1
    backlog: int = 2048
    reuse_port: bool = True


settings = Settings()


# ──────────────────────────────────────────────────────────────────────────────
# Contexts
# ──────────────────────────────────────────────────────────────────────────────


class ServerContext:
    """Server lifecycle context."""

    __slots__ = ("server",)

    def __init__(self, server: Server) -> None:
        self.server = server


# ──────────────────────────────────────────────────────────────────────────────
# Type Aliases
# ──────────────────────────────────────────────────────────────────────────────


class PluginProtocol(Protocol):
    """Plugin protocol: name + setup(app)."""

    name: str

    def setup(self, app: App) -> None: ...


RouteResult = ResponseHTTP | Response | None
AsyncRouteResult = Coroutine[Any, Any, RouteResult]
MiddlewareFn = Callable[[Request, ResponseHTTP], None]
RouteHandler = Callable[[Request], RouteResult | AsyncRouteResult]
HookHandler = Callable[[Any], None]
UploadHandler = Callable[[list[UploadedFile]], Response]


# ──────────────────────────────────────────────────────────────────────────────
# App
# ──────────────────────────────────────────────────────────────────────────────


@dataclass
class App:
    """Application: routing, middleware, hooks, plugins."""

    _hooks: dict[str, list[HookHandler]] = field(default_factory=dict)
    _middleware: list[MiddlewareFn] = field(default_factory=list)
    _router: RadixRouter = field(default_factory=RadixRouter)
    _routes: dict[str, dict[str, RouteHandler]] = field(
        default_factory=lambda: {m: {} for m in HTTPMethod}
    )
    _plugins: list[PluginProtocol] = field(default_factory=list)
    _routers: list[Router] = field(default_factory=list)
    state: SimpleNamespace = field(default_factory=SimpleNamespace)

    def plugin(self, p: PluginProtocol) -> App:
        """Register plugin."""
        self._plugins.append(p)
        p.setup(self)
        return self

    def on(self, event: str) -> Callable[[HookHandler], HookHandler]:
        """Decorator: register event hook."""

        def decorator(fn: HookHandler) -> HookHandler:
            self._hooks.setdefault(event, []).append(fn)
            return fn

        return decorator

    def use(self, fn: MiddlewareFn) -> MiddlewareFn:
        """Register middleware."""
        self._middleware.append(fn)
        return fn

    def _register_route(self, method: str, path: str, fn: RouteHandler) -> RouteHandler:
        """Register route."""
        self._router.add(method, path, fn)
        self._routes.setdefault(method, {})[path] = fn
        return fn

    def _route_decorator(
        self, method: str, path: str
    ) -> Callable[[RouteHandler], RouteHandler]:
        """Create route decorator."""

        def decorator(fn: RouteHandler) -> RouteHandler:
            return self._register_route(method, path, fn)

        return decorator

    def get(self, path: str) -> Callable[[RouteHandler], RouteHandler]:
        """GET route decorator."""
        return self._route_decorator(HTTPMethod.GET, path)

    def post(self, path: str) -> Callable[[RouteHandler], RouteHandler]:
        """POST route decorator."""
        return self._route_decorator(HTTPMethod.POST, path)

    def put(self, path: str) -> Callable[[RouteHandler], RouteHandler]:
        """PUT route decorator."""
        return self._route_decorator(HTTPMethod.PUT, path)

    def delete(self, path: str) -> Callable[[RouteHandler], RouteHandler]:
        """DELETE route decorator."""
        return self._route_decorator(HTTPMethod.DELETE, path)

    def patch(self, path: str) -> Callable[[RouteHandler], RouteHandler]:
        """PATCH route decorator."""
        return self._route_decorator(HTTPMethod.PATCH, path)

    def include_router(self, r: Router) -> App:
        """Include Router, registering all its routes."""
        r._app = self
        self._routers.append(r)
        for method, path, handler, _info in r._routes:
            self._register_route(method, path, handler)
        return self

    def emit(self, event: str, ctx: Any) -> Any:
        """Emit event to hooks."""
        for fn in self._hooks.get(event, []):
            fn(ctx)
            if getattr(ctx, "stop", False):
                break
        return ctx

    def run_middleware(self, req: Request, res: ResponseHTTP) -> bool:
        """Run middleware chain. Returns False if stopped."""
        for fn in self._middleware:
            fn(req, res)
            if res.stop:
                return False
        return True

    def route(self, method: str, path: str, req: Request) -> Response | None:
        """Match and execute route."""
        match = self._router.match_new(method, path)
        if not match.matched or match.handler is None:
            return None
        if match.params:
            req.params.update(match.params)
        result: RouteResult = match.handler(req)
        if isinstance(result, ResponseHTTP):
            return {
                "status": result.status,
                "body": result.body,
                "headers": result.headers,
            }
        return result

    def static(self, path_prefix: str) -> RouteHandler:
        """Register static file handler."""

        @self.get(f"{path_prefix}/*path")
        def handler(req: Request) -> Response:
            return file(settings.base_dir / "static" / req.params["path"])

        return handler


class Plugin:
    """Base plugin class."""

    name: str = "plugin"

    def setup(self, app: App) -> None:
        """Override to register hooks/routes."""
        raise NotImplementedError


# Global app and decorators
app = App()
on = app.on
use = app.use
get = app.get
post = app.post
put = app.put
delete = app.delete
patch = app.patch
static = app.static
include_router = app.include_router


# ──────────────────────────────────────────────────────────────────────────────
# Built-in Internal Endpoints
# ──────────────────────────────────────────────────────────────────────────────


@app.get("/_internal/health")
def _internal_health(_req: Request) -> Response:
    """Health check."""
    return {"body": {"status": "ok"}, "status": 200}


# ──────────────────────────────────────────────────────────────────────────────
# Event Loop
# ──────────────────────────────────────────────────────────────────────────────

_LOOP_POLICY: str = "stdlib"


@app.get("/_internal/info")
def _internal_info(_req: Request) -> Response:
    """Server info."""
    vi = sys.version_info
    return {
        "body": {
            "version": __version__,
            "python": f"{vi.major}.{vi.minor}.{vi.micro}",
            "event_loop": _LOOP_POLICY,
            "plugins": [p.name for p in app._plugins],
        },
        "status": 200,
    }


def upload(path: str, handler: UploadHandler) -> None:
    """Register upload endpoint."""
    app._routes[HTTPMethod.POST][path] = create_upload_handler(handler)


# ──────────────────────────────────────────────────────────────────────────────
# WebSocket
# ──────────────────────────────────────────────────────────────────────────────


def _process_ws_text(ws: WSClient | AsyncWSClient, data: bytes | str) -> None:
    """Process WebSocket text message."""
    try:
        text_data = data.decode(ENCODING_UTF8) if isinstance(data, bytes) else data
        msg = _json_loads(text_data)
        ctx = WSMessage(ws=ws, data=msg, reply=None, stop=False)
        app.emit(Hook.WS_MESSAGE, ctx)
        if ctx.stop:
            return
        ws.send(ctx.reply if ctx.reply is not None else {WS_DEFAULT_ECHO_KEY: msg})
    except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
        ws.send({"error": WS_ERROR_INVALID_JSON})


def _handle_ws_loop(ws: WSClient) -> None:
    """Sync WebSocket message loop."""
    app.emit(Hook.WS_CONNECT, WSContext(ws))
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
                _process_ws_text(ws, data)
    except (ConnectionResetError, BrokenPipeError, OSError):
        pass
    finally:
        app.emit(Hook.WS_DISCONNECT, WSContext(ws))
        ws.close()


def handle_ws_upgrade(sock: socket.socket, ws_key: str) -> None:
    """Handle WebSocket upgrade."""
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, SOCKET_BUFFER_SYNC)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, SOCKET_BUFFER_SYNC)
    ws = WSClient(sock)
    ws.handshake_with_key(ws_key)
    _handle_ws_loop(ws)


def _setup_event_loop() -> None:
    """Setup best available event loop."""
    global _LOOP_POLICY
    for name, module in [("uvloop", "uvloop"), ("winloop", "winloop")]:
        try:
            loop_mod = __import__(module)
            asyncio.set_event_loop_policy(loop_mod.EventLoopPolicy())
            _LOOP_POLICY = name
            return
        except ImportError:
            pass
    _LOOP_POLICY = "stdlib"


# ──────────────────────────────────────────────────────────────────────────────
# Async HTTP Protocol
# ──────────────────────────────────────────────────────────────────────────────


class AsyncHTTPProtocol(asyncio.Protocol):
    """Asyncio HTTP protocol handler."""

    def __init__(self) -> None:
        self.transport: asyncio.Transport | None = None
        self.buffer: bytearray = bytearray()
        self.keep_alive: bool = True
        self.request_count: int = 0
        self._ws_client: AsyncWSClient | None = None
        self._ws_mode: bool = False

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]
        sock = transport.get_extra_info("socket")
        if sock:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            with contextlib.suppress(OSError):
                sock.setsockopt(
                    socket.SOL_SOCKET, socket.SO_RCVBUF, SOCKET_BUFFER_ASYNC
                )
                sock.setsockopt(
                    socket.SOL_SOCKET, socket.SO_SNDBUF, SOCKET_BUFFER_ASYNC
                )

    def connection_lost(self, exc: Exception | None) -> None:
        del exc  # unused but required by Protocol
        self.transport = None

    async def _ws_message_loop(self, ws: AsyncWSClient) -> None:
        """Async WebSocket message loop."""
        while True:
            opcode, data = await ws.recv_async()
            if opcode is None or opcode == WSOpcode.CLOSE:
                ws.send(b"", WSOpcode.CLOSE)
                break
            if opcode == WSOpcode.PING:
                ws.send(data or b"", WSOpcode.PONG)
                continue
            if opcode == WSOpcode.TEXT and data is not None:
                _process_ws_text(ws, data)

    async def _handle_websocket(self, ws_key: str) -> None:
        """Handle WebSocket upgrade."""
        if not self.transport:
            return
        accept = b64encode(sha1(ws_key.encode() + WS_MAGIC).digest()).decode()
        self.transport.write(
            f"HTTP/1.1 101 Switching Protocols\r\n"
            f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n\r\n".encode()
        )
        ws = AsyncWSClient(self.transport)
        ws.data = SimpleNamespace()
        self._ws_client = ws
        self._ws_mode = True
        app.emit(Hook.WS_CONNECT, WSContext(ws))
        try:
            await self._ws_message_loop(ws)
        except (ConnectionResetError, BrokenPipeError, OSError, asyncio.CancelledError):
            pass
        finally:
            ws.mark_closed()
            self._ws_mode = False
            self._ws_client = None
            app.emit(Hook.WS_DISCONNECT, WSContext(ws))
            if self.transport and not self.transport.is_closing():
                self.transport.close()

    def _send_response(self, res: ResponseHTTP) -> None:
        """Send HTTP response."""
        if not self.transport:
            return
        content_type = res.headers.get(HEADER_CONTENT_TYPE)
        is_json = content_type is None or content_type == CONTENT_TYPE_JSON
        if is_json:
            body = _json_dumps_cached(res.body)
            content_type = CONTENT_TYPE_JSON
        elif isinstance(res.body, bytes):
            body = res.body
        else:
            body = str(res.body).encode()

        status_line = PREBUILT_STATUS_HEADERS.get(res.status)
        if status_line is None:
            status_text = HTTP_STATUS.get(res.status, "OK")
            status_line = f"HTTP/1.1 {res.status} {status_text}\r\n".encode()

        parts = [status_line]
        if is_json:
            parts.append(HEADER_JSON_BYTES)
        else:
            parts.append(f"Content-Type: {content_type}\r\n".encode())
        parts.append(f"Content-Length: {len(body)}\r\n".encode())
        for k, v in res.headers.items():
            if k != HEADER_CONTENT_TYPE:
                parts.append(f"{k}: {v}\r\n".encode())
        parts.append(HEADER_KEEPALIVE_BYTES if self.keep_alive else HEADER_CLOSE_BYTES)
        parts.append(b"\r\n")
        parts.append(body)
        self.transport.write(b"".join(parts))

    async def _handle_request(
        self, method: str, path: str, headers: dict[str, str], body_data: bytes
    ) -> None:
        """Handle HTTP request."""
        if not self.transport:
            return
        path_clean, query = parse_query(path)

        # Parse body
        content_type = headers.get(HEADER_CONTENT_TYPE, "")
        if CONTENT_TYPE_MULTIPART in content_type:
            body: Any = body_data
        elif body_data:
            try:
                body = _json_loads(body_data)
            except (json.JSONDecodeError, ValueError):
                body = body_data.decode(ENCODING_UTF8, errors="replace")
        else:
            body = None

        req = Request(
            method=method, path=path_clean, headers=headers, query=query, body=body
        )
        res = ResponseHTTP(status=200, body={}, headers={}, stop=False)

        # Middleware
        for mw in app._middleware:
            mw(req, res)
            if res.stop:
                self._send_response(res)
                return

        # Pre-hooks
        hook_key = _HTTP_HOOK_KEYS.get(method)
        if hook_key:
            for fn in app._hooks.get(hook_key, []):
                fn(HTTPContext(req, res))
                if res.stop:
                    self._send_response(res)
                    return

        # Route matching
        match = app._router.match_new(method, path_clean)
        if match.matched and match.handler is not None:
            if match.params:
                req.params.update(match.params)
            result = match.handler(req)
            if inspect.iscoroutine(result):
                result = await result
            if result:
                if isinstance(result, ResponseHTTP):
                    res.status, res.body = result.status, result.body
                    res.headers.update(result.headers)
                else:
                    res.status = result.get("status", res.status)
                    res.body = result.get("body", res.body)
                    if h := result.get("headers"):
                        res.headers.update(h)
        elif not res.body:
            res.body = {"method": method, "path": path_clean}

        # Post-hooks
        if hook_key:
            for fn in app._hooks.get(hook_key + HOOK_AFTER_SUFFIX, []):
                fn(HTTPContext(req, res))

        self._send_response(res)

    def _send_error(self, status: int) -> None:
        """Send error response."""
        if self.transport:
            status_text = HTTP_STATUS.get(status, "Error")
            body = f'{{"error":"{status_text}"}}'
            self.transport.write(
                f"HTTP/1.1 {status} {status_text}\r\n"
                f"Content-Type: {CONTENT_TYPE_JSON}\r\n"
                f"Content-Length: {len(body)}\r\n"
                f"Connection: close\r\n\r\n{body}".encode()
            )
            self.transport.close()

    def _process_buffer(self) -> None:
        """Process HTTP requests from buffer."""
        while b"\r\n\r\n" in self.buffer:
            header_end = self.buffer.find(b"\r\n\r\n")
            if header_end == -1:
                return
            header_data = bytes(self.buffer[:header_end])
            body_start = header_end + 4
            lines = header_data.split(b"\r\n")
            if not lines:
                self._send_error(400)
                return
            parts = lines[0].decode("latin-1").split(" ", 2)
            if len(parts) != 3:
                self._send_error(400)
                return
            method, path, _ = parts
            headers = {}
            for line in lines[1:]:
                if b": " in line:
                    k, v = line.split(b": ", 1)
                    headers[k.decode("latin-1")] = v.decode("latin-1")

            # WebSocket upgrade
            is_ws = headers.get("Upgrade", "").lower() == "websocket"
            if path == settings.ws_path and is_ws:
                ws_key = headers.get("Sec-WebSocket-Key")
                if ws_key and self.transport:
                    del self.buffer[:body_start]
                    asyncio.create_task(self._handle_websocket(ws_key))
                    return

            content_length = int(headers.get("Content-Length", "0"))
            if len(self.buffer) < body_start + content_length:
                return
            body_data = bytes(self.buffer[body_start : body_start + content_length])
            del self.buffer[: body_start + content_length]
            self.request_count += 1
            asyncio.create_task(self._handle_request(method, path, headers, body_data))

    def data_received(self, data: bytes) -> None:
        if self._ws_mode and self._ws_client:
            self._ws_client.feed_data(data)
            return
        self.buffer.extend(data)
        self._process_buffer()


# ──────────────────────────────────────────────────────────────────────────────
# Server
# ──────────────────────────────────────────────────────────────────────────────


async def _run_async_server(host: str, port: int) -> None:
    """Run async server."""
    loop = asyncio.get_event_loop()
    server = await loop.create_server(
        AsyncHTTPProtocol,
        host,
        port,
        reuse_address=True,
        reuse_port=SO_REUSEPORT is not None and settings.reuse_port,
        backlog=settings.backlog,
    )
    if settings.debug:
        print(f"[AsyncServer] http://{host}:{port} (event loop: {_LOOP_POLICY})")
        print(f"[WebSocket] ws://{host}:{port}{settings.ws_path}")
    async with server:
        await server.serve_forever()


def _async_worker_process(host: str, port: int, worker_id: int) -> None:
    """Worker process."""
    _setup_event_loop()
    if settings.debug:
        print(f"[Worker {worker_id}] Starting on {host}:{port}")
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_run_async_server(host, port))


@dataclass
class Server:
    """HTTP + WebSocket server."""

    _running: bool = field(default=False, repr=False)
    _workers: list[multiprocessing.Process] = field(default_factory=list, repr=False)
    _loop: asyncio.AbstractEventLoop | None = field(default=None, repr=False)
    _server: asyncio.AbstractServer | None = field(default=None, repr=False)

    async def _run_managed(self, host: str, port: int) -> None:
        """Run with managed lifecycle."""
        self._server = await self._loop.create_server(  # type: ignore[union-attr]
            AsyncHTTPProtocol,
            host,
            port,
            reuse_address=True,
            reuse_port=SO_REUSEPORT is not None and settings.reuse_port,
            backlog=settings.backlog,
        )
        if settings.debug:
            print(f"[AsyncServer] http://{host}:{port} (event loop: {_LOOP_POLICY})")
            print(f"[WebSocket] ws://{host}:{port}{settings.ws_path}")
        async with self._server:
            await self._server.serve_forever()

    def start(self) -> None:
        """Start server."""
        self._running = True
        app.emit(Hook.SERVER_START, ServerContext(self))
        if settings.workers > 1 and sys.platform != "win32":
            for i in range(settings.workers):
                p = multiprocessing.Process(
                    target=_async_worker_process,
                    args=(settings.host, settings.port, i),
                    daemon=True,
                )
                p.start()
                self._workers.append(p)
        else:
            _setup_event_loop()
            self._loop = asyncio.new_event_loop()

            def run() -> None:
                asyncio.set_event_loop(self._loop)
                with contextlib.suppress(KeyboardInterrupt, asyncio.CancelledError):
                    self._loop.run_until_complete(  # type: ignore[union-attr]
                        self._run_managed(settings.host, settings.port)
                    )

            threading.Thread(target=run, daemon=True).start()

    def stop(self) -> None:
        """Stop server."""
        self._running = False
        if self._server is not None and self._loop is not None:
            self._loop.call_soon_threadsafe(self._server.close)
        app.emit(Hook.SERVER_STOP, ServerContext(self))
        for p in self._workers:
            p.terminate()
            p.join(timeout=1)
        self._workers.clear()

    @staticmethod
    def run(plugin: list[Plugin] | None = None) -> None:
        """Run server with optional plugins."""
        if plugin:
            for p in plugin:
                app.plugin(p)
        server = Server()
        server.start()
        try:
            while server._running:
                threading.Event().wait(SERVER_MAIN_THREAD_WAIT)
        except KeyboardInterrupt:
            pass
        finally:
            server.stop()


# ──────────────────────────────────────────────────────────────────────────────
# Exports
# ──────────────────────────────────────────────────────────────────────────────

__all__ = [
    "__version__",
    # Enums
    "HTTPMethod",
    "WSOpcode",
    # Content Types
    "CONTENT_TYPE_JSON",
    "CONTENT_TYPE_HTML",
    "CONTENT_TYPE_TEXT",
    "CONTENT_TYPE_OCTET",
    # Core
    "Server",
    "Settings",
    "settings",
    "App",
    "Plugin",
    # Request/Response
    "Request",
    "ResponseHTTP",
    "Response",
    "UploadedFile",
    # WebSocket
    "WSClient",
    "AsyncWSClient",
    "WSMessage",
    "WSContext",
    # Context
    "HTTPContext",
    "ServerContext",
    "Route",
    # Router
    "Router",
    "RouteInfo",
    "router",
    "RadixRouter",
    "RouteMatch",
    # Global decorators
    "app",
    "get",
    "post",
    "put",
    "delete",
    "patch",
    "on",
    "use",
    "static",
    "include_router",
    # Factories
    "make_request",
    "make_response",
    # Response helpers
    "html",
    "text",
    "error",
    "file",
    "upload",
    "clear_file_cache",
    # Type aliases
    "MiddlewareFn",
    "RouteHandler",
    "HookHandler",
    "UploadHandler",
    "PluginProtocol",
]
