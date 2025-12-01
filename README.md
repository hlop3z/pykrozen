# Lightweight HTTP + WebSocket Server

A minimal, extensible HTTP and WebSocket server with plugin support. Zero external dependencies.

## Quick Start

```python
from server import Server, get, post, on

@get("/hello")
def hello(req):
    return {"body": {"message": "Hello, World!"}}

server = Server()
server.start()
```

## Table of Contents

- [Installation](#installation)
- [Basic Usage](#basic-usage)
- [HTTP Routes](#http-routes)
- [Middleware](#middleware)
- [Hooks](#hooks)
- [WebSocket](#websocket)
- [Plugins](#plugins)
- [Shared State](#shared-state)
- [Type Reference](#type-reference)

---

## Installation

No installation required. Copy `server.py` to your project.

```python
from server import (
    Server, App, Plugin,
    Request, Response,
    WSClient, WSMessage, WSContext,
    app, get, post, on, use
)
```

---

## Basic Usage

### Starting the Server

```python
from server import Server

server = Server.run(port=8000, ws=8765, host="127.0.0.1")
```

### Configuration

```python
server = Server.run(
    port=3000,      # HTTP port (default: 8000)
    ws=3001,        # WebSocket port (default: 8765)
    host="0.0.0.0"  # Bind address (default: 127.0.0.1)
)
```

---

## HTTP Routes

### GET Routes

```python
from server import get, Request

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
from server import post, Request

@post("/users")
def create_user(req: Request) -> dict:
    username = req.body.get("username")
    return {
        "status": 201,
        "body": {"created": username}
    }
```

### Accessing Request Data

```python
@get("/search")
def search(req: Request) -> dict:
    # URL: /search?q=hello&limit=10

    # Path and method
    print(req.method)   # "GET"
    print(req.path)     # "/search"

    # Query parameters
    print(req.query)    # {"q": "hello", "limit": "10"}
    query = req.query.get("q", "")

    # Headers
    auth = req.headers.get("Authorization", "")

    # Body (POST only)
    data = req.body  # parsed JSON or string

    return {"body": {"query": query}}
```

### Response Format

Route handlers return a dict with optional keys:

```python
{
    "status": 200,           # HTTP status code (default: 200)
    "body": {"key": "value"},  # Response body (JSON serialized)
    "headers": {}            # Additional headers
}
```

---

## Middleware

Middleware runs on every HTTP request before route handlers.

### Basic Middleware

```python
from server import use, Request, Response

@use
def logger(req: Request, res: Response) -> None:
    print(f"{req.method} {req.path}")
```

### Authentication Middleware

```python
@use
def auth(req: Request, res: Response) -> None:
    if req.path.startswith("/api"):
        token = req.headers.get("Authorization")
        if token != "secret-token":
            res.status = 401
            res.body = {"error": "Unauthorized"}
            res.stop = True  # Stop middleware chain
```

### CORS Middleware

```python
@use
def cors(req: Request, res: Response) -> None:
    res.headers["Access-Control-Allow-Origin"] = "*"
    res.headers["Access-Control-Allow-Methods"] = "GET, POST"
```

### Rate Limiting

```python
from server import app
from time import time

app.state.requests = {}

@use
def rate_limit(req: Request, res: Response) -> None:
    ip = req.headers.get("X-Forwarded-For", "unknown")
    now = time()

    # Clean old entries
    app.state.requests = {
        k: v for k, v in app.state.requests.items()
        if now - v < 60
    }

    if ip in app.state.requests:
        res.status = 429
        res.body = {"error": "Too many requests"}
        res.stop = True
    else:
        app.state.requests[ip] = now
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
from server import on

@on("http:get")
def before_get(ctx):
    print(f"GET request to {ctx.req.path}")

@on("http:get:after")
def after_get(ctx):
    print(f"Response: {ctx.res.status}")
```

### Modifying Response in Hooks

```python
@on("http:post")
def validate_json(ctx):
    if ctx.req.body is None:
        ctx.res.status = 400
        ctx.res.body = {"error": "JSON body required"}
        ctx.res.stop = True
```

---

## WebSocket

### Handling Connections

```python
from server import on, WSContext

@on("ws:connect")
def on_connect(ctx: WSContext) -> None:
    print(f"Client connected")
    ctx.ws.data.username = "anonymous"  # Per-connection state

@on("ws:disconnect")
def on_disconnect(ctx: WSContext) -> None:
    print(f"Client {ctx.ws.data.username} disconnected")
```

### Handling Messages

```python
from server import on, WSMessage

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

### Sending Messages

```python
# Inside a hook
ctx.ws.send({"type": "notification", "text": "Hello!"})

# Send to specific client (store reference)
app.state.clients = []

@on("ws:connect")
def track_client(ctx: WSContext) -> None:
    app.state.clients.append(ctx.ws)

@on("ws:disconnect")
def untrack_client(ctx: WSContext) -> None:
    app.state.clients.remove(ctx.ws)

# Broadcast to all
def broadcast(message: dict) -> None:
    for client in app.state.clients:
        client.send(message)
```

### Chat Room Example

```python
from server import on, app, WSContext, WSMessage

app.state.rooms = {}  # room_name -> [ws clients]

@on("ws:message")
def chat_handler(ctx: WSMessage) -> None:
    action = ctx.data.get("action")

    if action == "join":
        room = ctx.data.get("room", "general")
        ctx.ws.data.room = room
        app.state.rooms.setdefault(room, []).append(ctx.ws)
        ctx.reply = {"action": "joined", "room": room}
        ctx.stop = True

    elif action == "message":
        room = getattr(ctx.ws.data, "room", None)
        if room and room in app.state.rooms:
            for client in app.state.rooms[room]:
                if client != ctx.ws:
                    client.send({
                        "action": "message",
                        "from": getattr(ctx.ws.data, "username", "anon"),
                        "text": ctx.data.get("text")
                    })
        ctx.reply = {"action": "sent"}
        ctx.stop = True

@on("ws:disconnect")
def leave_room(ctx: WSContext) -> None:
    room = getattr(ctx.ws.data, "room", None)
    if room and room in app.state.rooms:
        app.state.rooms[room].remove(ctx.ws)
```

---

## Plugins

Plugins encapsulate reusable functionality.

### Creating a Plugin

```python
from dataclasses import dataclass
from server import Plugin, App, Request, Response

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

        @app.get("/auth/check")
        def auth_check(req: Request) -> dict:
            return {"body": {"valid": True}}
```

### Using a Plugin

```python
from server import app, Server

app.plugin(AuthPlugin(secret="my-secret-token"))

server = Server()
server.start()
```

### Logger Plugin Example

```python
from dataclasses import dataclass
from datetime import datetime
from server import Plugin, App

@dataclass
class LoggerPlugin(Plugin):
    name: str = "logger"
    prefix: str = "[LOG]"

    def setup(self, app: App) -> None:
        @app.on("http:get")
        def log_get(ctx):
            self._log(f"GET {ctx.req.path}")

        @app.on("http:post")
        def log_post(ctx):
            self._log(f"POST {ctx.req.path}")

        @app.on("ws:connect")
        def log_ws_connect(ctx):
            self._log("WS connected")

        @app.on("ws:disconnect")
        def log_ws_disconnect(ctx):
            self._log("WS disconnected")

    def _log(self, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        print(f"{self.prefix} [{timestamp}] {message}")
```

### Metrics Plugin Example

```python
from dataclasses import dataclass, field
from server import Plugin, App
from types import SimpleNamespace

@dataclass
class MetricsPlugin(Plugin):
    name: str = "metrics"

    def setup(self, app: App) -> None:
        app.state.metrics = SimpleNamespace(
            requests=0,
            ws_connections=0,
            ws_messages=0
        )

        @app.use
        def count_requests(req, res):
            app.state.metrics.requests += 1

        @app.on("ws:connect")
        def count_connect(ctx):
            app.state.metrics.ws_connections += 1

        @app.on("ws:disconnect")
        def count_disconnect(ctx):
            app.state.metrics.ws_connections -= 1

        @app.on("ws:message")
        def count_messages(ctx):
            app.state.metrics.ws_messages += 1

        @app.get("/metrics")
        def get_metrics(req):
            m = app.state.metrics
            return {"body": {
                "requests": m.requests,
                "ws_connections": m.ws_connections,
                "ws_messages": m.ws_messages
            }}
```

---

## Shared State

Use `app.state` (a `SimpleNamespace`) for sharing data across handlers.

### Basic Usage

```python
from server import app
from types import SimpleNamespace

# Set values
app.state.users = {}
app.state.config = SimpleNamespace(
    debug=True,
    version="1.0.0"
)

# Access in handlers
@get("/config")
def get_config(req):
    return {"body": {
        "debug": app.state.config.debug,
        "version": app.state.config.version
    }}

@post("/users")
def create_user(req):
    user_id = len(app.state.users) + 1
    app.state.users[user_id] = req.body
    return {"status": 201, "body": {"id": user_id}}
```

### Per-Connection State

WebSocket clients have their own `data` namespace:

```python
@on("ws:connect")
def init_client(ctx: WSContext) -> None:
    ctx.ws.data.user_id = None
    ctx.ws.data.authenticated = False
    ctx.ws.data.joined_at = time()

@on("ws:message")
def handle_auth(ctx: WSMessage) -> None:
    if ctx.data.get("action") == "login":
        ctx.ws.data.user_id = ctx.data.get("user_id")
        ctx.ws.data.authenticated = True
```

---

## Type Reference

### Request

```python
class Request(SimpleNamespace):
    method: str           # "GET" or "POST"
    path: str             # URL path (e.g., "/users")
    headers: dict[str, str]  # Request headers
    query: dict[str, str]    # Query parameters
    body: Any             # Parsed JSON body or string
```

### Response

```python
class Response(SimpleNamespace):
    status: int           # HTTP status code
    body: Any             # Response body (JSON serialized)
    headers: dict[str, str]  # Response headers
    stop: bool            # Set True to stop middleware chain
```

### WSClient

```python
class WSClient:
    data: SimpleNamespace  # Per-connection state

    def send(self, data: Any, opcode: int = 1) -> None:
        """Send data (auto-serializes dicts to JSON)"""

    def close(self) -> None:
        """Close the connection"""
```

### WSMessage

```python
class WSMessage(SimpleNamespace):
    ws: WSClient     # The client connection
    data: Any        # Parsed message data
    reply: Any       # Set to send custom response
    stop: bool       # Set True to skip default echo
```

### Context Types

```python
from typing import NamedTuple

class HTTPContext(NamedTuple):
    req: Request
    res: Response

class WSContext(NamedTuple):
    ws: WSClient

class ServerContext(NamedTuple):
    server: Server
```

---

## Full Example

```python
from dataclasses import dataclass
from server import (
    Server, Plugin, App,
    Request, Response, WSMessage, WSContext,
    app, get, post, on, use
)
from types import SimpleNamespace
from time import time

#  Plugins

@dataclass
class AuthPlugin(Plugin):
    name: str = "auth"
    api_key: str = "secret"

    def setup(self, app: App) -> None:
        @app.use
        def check_api_key(req: Request, res: Response) -> None:
            if req.path.startswith("/api"):
                if req.headers.get("X-API-Key") != self.api_key:
                    res.status = 401
                    res.body = {"error": "Invalid API key"}
                    res.stop = True

#  State

app.state.users = {}
app.state.ws_clients = []

#  Middleware

@use
def logger(req: Request, res: Response) -> None:
    print(f"[{time():.0f}] {req.method} {req.path}")

#  Routes

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

#  WebSocket

@on("ws:connect")
def ws_connect(ctx: WSContext) -> None:
    app.state.ws_clients.append(ctx.ws)
    ctx.ws.data.connected_at = time()

@on("ws:disconnect")
def ws_disconnect(ctx: WSContext) -> None:
    app.state.ws_clients.remove(ctx.ws)

@on("ws:message")
def ws_message(ctx: WSMessage) -> None:
    if ctx.data.get("type") == "ping":
        ctx.reply = {"type": "pong", "time": time()}
        ctx.stop = True

#  Start

if __name__ == "__main__":
    app.plugin(AuthPlugin(api_key="my-secret-key"))

    server = Server(port=8000, ws=8765)
    server.start()

    import threading
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        server.stop()
        print("\nServer stopped")
```

---

## Testing

### HTTP Testing

```python
import urllib.request
import json

# GET request
response = urllib.request.urlopen("http://localhost:8000/")
data = json.loads(response.read())
print(data)

# POST request
req = urllib.request.Request(
    "http://localhost:8000/api/users",
    data=json.dumps({"name": "Alice"}).encode(),
    headers={"Content-Type": "application/json", "X-API-Key": "my-secret-key"}
)
response = urllib.request.urlopen(req)
data = json.loads(response.read())
print(data)
```

### WebSocket Testing (with websocket-client)

```python
import websocket
import json

ws = websocket.create_connection("ws://localhost:8765")
ws.send(json.dumps({"type": "ping"}))
result = json.loads(ws.recv())
print(result)  # {"type": "pong", "time": ...}
ws.close()
```

### WebSocket Testing (browser)

```javascript
const ws = new WebSocket("ws://localhost:8765");

ws.onopen = () => {
  ws.send(JSON.stringify({ type: "ping" }));
};

ws.onmessage = (event) => {
  console.log(JSON.parse(event.data));
};
```
