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
from pykrozen.http import (
    HTTPContext,
    Request,
    Response,
    ResponseDict,
    Route,
    UploadedFile,
    create_upload_handler,
    error,
    file,
    html,
    make_request,
    make_response,
    parse_query,
    text,
)
from pykrozen.router import RadixRouter, RouteMatch
from pykrozen.utils import _json_dumps_cached, _json_loads
from pykrozen.ws import AsyncWSClient, WSClient, WSContext, WSMessage

# Platform-specific socket option (Unix only)
SO_REUSEPORT: int | None = getattr(socket, "SO_REUSEPORT", None)

# Pre-computed hook keys to avoid string operations per-request
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
    """Global server configuration. Modify before calling `Server.run()`."""

    port: int = 8000
    host: str = "127.0.0.1"
    ws_path: str = "/ws"
    debug: bool = False
    base_dir: Path = Path.cwd()
    workers: int = 1
    backlog: int = 2048
    reuse_port: bool = True


settings = Settings()


# ──────────────────────────────────────────────────────────────────────────────
# Server Context
# ──────────────────────────────────────────────────────────────────────────────


class ServerContext:
    """Server lifecycle context."""

    __slots__ = ("server",)

    def __init__(self, server: Server) -> None:
        self.server = server


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
    """Application with routing, middleware, and hooks."""

    _hooks: dict[str, list[HookHandler]] = field(default_factory=dict)
    _middleware: list[MiddlewareFn] = field(default_factory=list)
    _router: RadixRouter = field(default_factory=RadixRouter)
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
        match = self._router.match_new(method, path)
        if not match.matched or match.handler is None:
            return None

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


def upload(path: str, handler: UploadHandler) -> None:
    """Register an upload endpoint for multipart file uploads."""
    app._routes[HTTPMethod.POST][path] = create_upload_handler(handler, app)


# ──────────────────────────────────────────────────────────────────────────────
# WebSocket Message Handling
# ──────────────────────────────────────────────────────────────────────────────


def _handle_ws_loop(ws: WSClient) -> None:
    """Handle WebSocket message loop."""
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
                try:
                    text_data = (
                        data.decode(ENCODING_UTF8) if isinstance(data, bytes) else data
                    )
                    msg = _json_loads(text_data)
                    ctx = WSMessage(ws=ws, data=msg, reply=None, stop=False)
                    app.emit(Hook.WS_MESSAGE, ctx)

                    if ctx.stop:
                        continue
                    if ctx.reply is not None:
                        ws.send(ctx.reply)
                    else:
                        ws.send({WS_DEFAULT_ECHO_KEY: msg})
                except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
                    ws.send({"error": WS_ERROR_INVALID_JSON})

    except (ConnectionResetError, BrokenPipeError, OSError):
        pass
    finally:
        app.emit(Hook.WS_DISCONNECT, WSContext(ws))
        ws.close()


def handle_ws_upgrade(sock: socket.socket, ws_key: str) -> None:
    """Handle WebSocket upgrade from HTTP."""
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, SOCKET_BUFFER_SYNC)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, SOCKET_BUFFER_SYNC)

    ws = WSClient(sock)
    ws.handshake_with_key(ws_key)
    _handle_ws_loop(ws)


# ──────────────────────────────────────────────────────────────────────────────
# Event Loop Selection
# ──────────────────────────────────────────────────────────────────────────────

_LOOP_POLICY: str = "stdlib"


def _setup_event_loop() -> None:
    """Set up the best available event loop policy."""
    global _LOOP_POLICY

    try:
        import uvloop

        asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
        _LOOP_POLICY = "uvloop"
        return
    except ImportError:
        pass

    try:
        import winloop

        asyncio.set_event_loop_policy(winloop.EventLoopPolicy())
        _LOOP_POLICY = "winloop"
        return
    except ImportError:
        pass

    _LOOP_POLICY = "stdlib"


# ──────────────────────────────────────────────────────────────────────────────
# Async HTTP Protocol
# ──────────────────────────────────────────────────────────────────────────────


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
        self.transport = transport  # type: ignore[assignment]
        sock = transport.get_extra_info("socket")
        if sock:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            try:
                sock.setsockopt(
                    socket.SOL_SOCKET, socket.SO_RCVBUF, SOCKET_BUFFER_ASYNC
                )
                sock.setsockopt(
                    socket.SOL_SOCKET, socket.SO_SNDBUF, SOCKET_BUFFER_ASYNC
                )
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
                        data.decode(ENCODING_UTF8) if isinstance(data, bytes) else data
                    )
                    msg = _json_loads(text_data)
                    ctx = WSMessage(ws=ws, data=msg, reply=None, stop=False)
                    app.emit(Hook.WS_MESSAGE, ctx)

                    if ctx.stop:
                        continue
                    if ctx.reply is not None:
                        ws.send(ctx.reply)
                    else:
                        ws.send({WS_DEFAULT_ECHO_KEY: msg})
                except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
                    ws.send({"error": WS_ERROR_INVALID_JSON})

    async def _handle_websocket(self, ws_key: str, headers: dict[str, str]) -> None:
        """Handle WebSocket upgrade asynchronously."""
        if not self.transport:
            return

        accept = b64encode(sha1(ws_key.encode() + WS_MAGIC).digest()).decode()
        response = (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
        )
        self.transport.write(response.encode())

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

    def _send_response(self, res: Response) -> None:
        """Send HTTP response using pre-built headers for speed."""
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
        """Handle HTTP request asynchronously."""
        if not self.transport:
            return

        path_clean, query = parse_query(path)

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
        res = Response(status=200, body={}, headers={}, stop=False)

        for mw in app._middleware:
            mw(req, res)
            if res.stop:
                self._send_response(res)
                return

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

        match = app._router.match_new(method, path_clean)
        if match.matched and match.handler is not None:
            if match.params:
                req.params.update(match.params)

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

        if hook_key:
            after_hooks = app._hooks.get(hook_key + HOOK_AFTER_SUFFIX)
            if after_hooks:
                ctx = HTTPContext(req, res)
                for fn in after_hooks:
                    fn(ctx)

        self._send_response(res)

    def _send_error(self, status: int) -> None:
        """Send error response."""
        if self.transport:
            status_text = HTTP_STATUS.get(status, "Error")
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
            header_end = self.buffer.find(b"\r\n\r\n")
            if header_end == -1:
                return

            header_data = bytes(self.buffer[:header_end])
            body_start = header_end + 4

            lines = header_data.split(b"\r\n")
            if not lines:
                self._send_error(400)
                return

            request_line = lines[0].decode("latin-1")
            parts = request_line.split(" ", 2)
            if len(parts) != 3:
                self._send_error(400)
                return

            method, path, _ = parts

            headers: dict[str, str] = {}
            for line in lines[1:]:
                if b": " in line:
                    key, val = line.split(b": ", 1)
                    headers[key.decode("latin-1")] = val.decode("latin-1")

            ws_upgrade = headers.get("Upgrade", "").lower() == "websocket"
            if path == settings.ws_path and ws_upgrade:
                ws_key = headers.get("Sec-WebSocket-Key")
                if ws_key and self.transport:
                    del self.buffer[:body_start]
                    asyncio.create_task(self._handle_websocket(ws_key, headers))
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


async def _run_async_server(host: str, port: int) -> None:
    """Run the async HTTP server."""
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
    """HTTP + WebSocket server with start/stop lifecycle and multiprocessing."""

    _running: bool = field(default=False, repr=False)
    _workers: list[multiprocessing.Process] = field(default_factory=list, repr=False)

    def start(self) -> None:
        """Start the server with optional worker processes."""
        self._running = True
        app.emit(Hook.SERVER_START, ServerContext(self))

        num_workers = settings.workers

        if num_workers > 1 and sys.platform != "win32":
            for i in range(num_workers):
                p = multiprocessing.Process(
                    target=_async_worker_process,
                    args=(settings.host, settings.port, i),
                    daemon=True,
                )
                p.start()
                self._workers.append(p)
        else:
            _setup_event_loop()

            def run_async() -> None:
                with contextlib.suppress(KeyboardInterrupt):
                    asyncio.run(_run_async_server(settings.host, settings.port))

            threading.Thread(target=run_async, daemon=True).start()

    def stop(self) -> None:
        """Stop the server and all workers."""
        self._running = False
        app.emit(Hook.SERVER_STOP, ServerContext(self))

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

        try:
            while server._running:
                threading.Event().wait(SERVER_MAIN_THREAD_WAIT)
        except KeyboardInterrupt:
            pass
        finally:
            server.stop()


__version__ = "0.1.0"
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
    "AsyncWSClient",
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
