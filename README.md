# Pykrozen

**Lightweight HTTP + WebSocket server with plugin system.**

A minimal, zero-dependency HTTP and WebSocket server designed for development and prototyping. Features middleware, routing, plugins, and WebSocket support out of the box.

## Quick Start

```python
from pykrozen import Server, settings, get

@get("/hello")
def hello(req):
    return {"body": {"message": "Hello, World!"}}

settings.port = 8000
settings.ws_path = "/ws"

Server.run()
```

## Table of Contents

- [Installation](#installation)
- [Configuration](#configuration)
- [HTTP Routes](#http-routes)
- [Middleware](#middleware)
- [Hooks](#hooks)
- [WebSocket](#websocket)
- [Plugins](#plugins)
- [Shared State](#shared-state)
- [Type Reference](#type-reference)
- [Performance](#performance)

---

## Installation

```bash
pip install pykrozen
```

```python
from pykrozen import (
    Server, Settings, settings,
    App, Plugin,
    Request, Response,
    WSClient, WSMessage, WSContext,
    app, get, post, on, use
)
```

---

## Configuration

All configuration is done via the global `settings` object before calling `Server.run()`:

```python
from pykrozen import settings, Server

# Server settings
settings.port = 3000          # HTTP port (default: 8000)
settings.host = "0.0.0.0"     # Bind address (default: 127.0.0.1)
settings.ws_path = "/socket"  # WebSocket path (default: /ws)

Server.run()
```

### Optional Performance Boost

For a significant throughput increase (often 2–4×), install an optimized event loop implementation:

**Unix/Linux/macOS:**

```bash
pip install uvloop
```

**Windows:**

```bash
pip install winloop
```

These packages replace the default event loop with a faster, drop-in alternative.

### Settings Reference

| Setting      | Type   | Default     | Description                            |
| ------------ | ------ | ----------- | -------------------------------------- |
| `port`       | `int`  | `8000`      | HTTP server port                       |
| `host`       | `str`  | `127.0.0.1` | Bind address                           |
| `ws_path`    | `str`  | `/ws`       | WebSocket upgrade path                 |
| `debug`      | `bool` | `False`     | Enable debug output                    |
| `base_dir`   | `Path` | `cwd()`     | Base directory for static files        |
| `workers`    | `int`  | `1`         | Number of worker processes (Unix only) |
| `backlog`    | `int`  | `2048`      | Socket listen backlog                  |
| `reuse_port` | `bool` | `True`      | Enable SO_REUSEPORT for load balancing |

WebSocket connections are handled on the **same port** as HTTP, accessible at the configured `ws_path`.

---

## HTTP Routes

### GET Routes

```python
from pykrozen import get, Request

@get("/")
def index(req: Request) -> dict:
    return {"body": {"message": "Welcome!"}}

@get("/users")
def list_users(req: Request) -> dict:
    return {
        "status": 200,
        "body": {"users": ["alice", "bob"]},
        "headers": {"X-Custom": "value"}
    }
```

### POST Routes

```python
from pykrozen import post, Request

@post("/users")
def create_user(req: Request) -> dict:
    username = req.body.get("username")
    return {
        "status": 201,
        "body": {"created": username}
    }
```

### Route Parameters

Dynamic segments using `:param` syntax:

```python
@get("/users/:id")
def get_user(req: Request) -> dict:
    user_id = req.params["id"]
    return {"body": {"user_id": user_id}}

@get("/posts/:post_id/comments/:comment_id")
def get_comment(req: Request) -> dict:
    return {"body": {
        "post": req.params["post_id"],
        "comment": req.params["comment_id"]
    }}
```

### Wildcard Routes

Capture remaining path with `*name` syntax:

```python
@get("/files/*filepath")
def serve_file(req: Request) -> dict:
    # /files/docs/readme.txt -> filepath = "docs/readme.txt"
    return {"body": {"path": req.params["filepath"]}}
```

### Query Parameters

```python
@get("/search")
def search(req: Request) -> dict:
    # URL: /search?q=hello&limit=10
    query = req.query.get("q", "")
    limit = int(req.query.get("limit", "20"))
    return {"body": {"query": query, "limit": limit}}
```

### Response Format

Route handlers return a dict with optional keys:

```python
{
    "status": 200,             # HTTP status code (default: 200)
    "body": {"key": "value"},  # Response body (JSON serialized)
    "headers": {}              # Additional headers
}
```

---

## Middleware

Middleware runs on every HTTP request before route handlers.

```python
from pykrozen import use, Request, Response

@use
def logger(req: Request, res: Response) -> None:
    print(f"{req.method} {req.path}")

@use
def auth(req: Request, res: Response) -> None:
    if req.path.startswith("/api"):
        token = req.headers.get("Authorization")
        if token != "secret-token":
            res.status = 401
            res.body = {"error": "Unauthorized"}
            res.stop = True  # Stop middleware chain

@use
def cors(req: Request, res: Response) -> None:
    res.headers["Access-Control-Allow-Origin"] = "*"
```

---

## Hooks

Hooks let you react to events at specific points in the request lifecycle.

### Available Events

| Event             | Context         | Description                   |
| ----------------- | --------------- | ----------------------------- |
| `http:get`        | `HTTPContext`   | Before GET route handler      |
| `http:post`       | `HTTPContext`   | Before POST route handler     |
| `http:get:after`  | `HTTPContext`   | After GET route handler       |
| `http:post:after` | `HTTPContext`   | After POST route handler      |
| `ws:connect`      | `WSContext`     | WebSocket client connected    |
| `ws:message`      | `WSMessage`     | WebSocket message received    |
| `ws:disconnect`   | `WSContext`     | WebSocket client disconnected |
| `server:start`    | `ServerContext` | Server started                |
| `server:stop`     | `ServerContext` | Server stopping               |

### HTTP Hooks

```python
from pykrozen import on

@on("http:get")
def before_get(ctx):
    print(f"GET request to {ctx.req.path}")

@on("http:get:after")
def after_get(ctx):
    print(f"Response: {ctx.res.status}")
```

---

## WebSocket

WebSocket connections are handled on the same port as HTTP, at the path configured in `settings.ws_path` (default: `/ws`).

### Handling Connections

```python
from pykrozen import on, WSContext

@on("ws:connect")
def on_connect(ctx: WSContext) -> None:
    print("Client connected")
    ctx.ws.data.username = "anonymous"  # Per-connection state

@on("ws:disconnect")
def on_disconnect(ctx: WSContext) -> None:
    print(f"Client {ctx.ws.data.username} disconnected")
```

### Handling Messages

```python
from pykrozen import on, WSMessage

@on("ws:message")
def on_message(ctx: WSMessage) -> None:
    msg_type = ctx.data.get("type")

    if msg_type == "ping":
        ctx.reply = {"type": "pong"}
        ctx.stop = True

    elif msg_type == "set_name":
        ctx.ws.data.username = ctx.data.get("name", "anonymous")
        ctx.reply = {"type": "name_set", "name": ctx.ws.data.username}
        ctx.stop = True

    # If stop=False and reply=None, default echo response is sent
```

### Broadcasting

```python
from pykrozen import app, on, WSContext

app.state.clients = []

@on("ws:connect")
def track_client(ctx: WSContext) -> None:
    app.state.clients.append(ctx.ws)

@on("ws:disconnect")
def untrack_client(ctx: WSContext) -> None:
    app.state.clients.remove(ctx.ws)

def broadcast(message: dict) -> None:
    for client in app.state.clients:
        client.send(message)
```

---

## Plugins

Plugins encapsulate reusable functionality.

### Creating a Plugin

```python
from dataclasses import dataclass
from pykrozen import Plugin, App, Request, Response

@dataclass
class AuthPlugin(Plugin):
    name: str = "auth"
    secret: str = "default-secret"

    def setup(self, app: App) -> None:
        @app.use
        def check_auth(req: Request, res: Response) -> None:
            if req.path.startswith("/api"):
                if req.headers.get("Authorization") != self.secret:
                    res.status = 401
                    res.body = {"error": "Unauthorized"}
                    res.stop = True
```

### Using a Plugin

```python
from pykrozen import app, Server

app.plugin(AuthPlugin(secret="my-secret-token"))
Server.run()
```

---

## Shared State

Use `app.state` for sharing data across handlers.

```python
from pykrozen import app, get

app.state.users = {}

@get("/users")
def list_users(req):
    return {"body": {"users": list(app.state.users.values())}}
```

### Per-Connection State

WebSocket clients have their own `data` namespace:

```python
@on("ws:connect")
def init_client(ctx: WSContext) -> None:
    ctx.ws.data.user_id = None
    ctx.ws.data.authenticated = False
```

---

## Type Reference

### Request

```python
class Request:
    method: str              # "GET", "POST", etc.
    path: str                # URL path
    headers: dict[str, str]  # Request headers
    query: dict[str, str]    # Query parameters
    params: dict[str, str]   # Route parameters (:id, *path)
    body: Any                # Parsed JSON body
    extra: dict[str, Any]    # Custom data from middleware
```

### Response

```python
class Response:
    status: int              # HTTP status code
    body: Any                # Response body
    headers: dict[str, str]  # Response headers
    stop: bool               # Stop middleware chain
```

### WSClient

```python
class WSClient:
    data: SimpleNamespace    # Per-connection state

    def send(self, data: Any, opcode: int = 1) -> None: ...
    def close(self) -> None: ...
```

### WSMessage

```python
class WSMessage:
    ws: WSClient      # Client connection
    data: Any         # Parsed message data
    reply: Any        # Set to send response
    stop: bool        # Skip default echo
```

---

## Full Example

```python
from dataclasses import dataclass
from pykrozen import (
    Server, settings, Plugin, App,
    Request, Response, WSMessage, WSContext,
    app, get, post, on, use
)
from time import time

# Configure
settings.port = 8000
settings.ws_path = "/ws"

# State
app.state.users = {}
app.state.ws_clients = []

# Middleware
@use
def logger(req: Request, res: Response) -> None:
    print(f"[{time():.0f}] {req.method} {req.path}")

# Routes
@get("/")
def index(req: Request) -> dict:
    return {"body": {"message": "Welcome to the API"}}

@get("/api/users")
def list_users(req: Request) -> dict:
    return {"body": {"users": list(app.state.users.values())}}

@post("/api/users")
def create_user(req: Request) -> dict:
    user_id = str(len(app.state.users) + 1)
    user = {"id": user_id, **req.body}
    app.state.users[user_id] = user

    # Notify WebSocket clients
    for ws in app.state.ws_clients:
        ws.send({"event": "user_created", "user": user})

    return {"status": 201, "body": user}

# WebSocket
@on("ws:connect")
def ws_connect(ctx: WSContext) -> None:
    app.state.ws_clients.append(ctx.ws)

@on("ws:disconnect")
def ws_disconnect(ctx: WSContext) -> None:
    app.state.ws_clients.remove(ctx.ws)

@on("ws:message")
def ws_message(ctx: WSMessage) -> None:
    if ctx.data.get("type") == "ping":
        ctx.reply = {"type": "pong", "time": time()}
        ctx.stop = True

# Start
if __name__ == "__main__":
    Server.run()
```

---

## Performance

Pykrozen is optimized for high throughput using only Python's standard library.

### Benchmarks

Typical performance on a modern machine (50 concurrent connections, 5 second test):

| Test      | Requests/sec | Avg Latency | Success Rate |
| --------- | ------------ | ----------- | ------------ |
| HTTP GET  | ~4,400 RPS   | ~11ms       | 100%         |
| HTTP POST | ~4,800 RPS   | ~10ms       | 100%         |
| WebSocket | ~5,900 RPS   | ~8ms        | 100%         |

### Performance Tuning

```python
from pykrozen import settings

# Multi-worker mode (Unix/Linux only)
# Each worker is a separate process sharing the same port
settings.workers = 4  # Use 4 worker processes

# Socket tuning
settings.backlog = 2048     # Increase connection queue
settings.reuse_port = True  # Enable SO_REUSEPORT (default)
```

### Optimizations

Pykrozen includes several performance optimizations:

1. **Static Route Cache** - O(1) lookup for exact path matches (no `:param` or `*wildcard`)
2. **Thread-local Route Matching** - Reuses match objects to avoid allocations
3. **64-bit XOR Unmask** - WebSocket payload decoding processes 8 bytes at a time
4. **JSON Response Caching** - Common small responses are cached to avoid re-serialization
5. **Socket Buffer Tuning** - Optimized TCP buffer sizes and `SO_REUSEADDR`
6. **Increased Listen Backlog** - Handles connection bursts (default: 2048)

### Multi-Worker Mode

On Unix/Linux systems, you can spawn multiple worker processes that share the same port using `SO_REUSEPORT`:

```python
import os
from pykrozen import settings, Server

# Use number of CPU cores
settings.workers = os.cpu_count() or 4
settings.debug = True  # See worker startup messages

Server.run()
```

Each worker runs its own HTTP server and the kernel load-balances connections between them.

> **Note:** Multi-worker mode is only available on Unix/Linux due to `SO_REUSEPORT` requirements. On Windows, the server runs in single-process threaded mode.
