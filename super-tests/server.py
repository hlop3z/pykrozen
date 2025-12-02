#!/usr/bin/env python3
"""
Sample pykrozen server with multiple endpoints for testing.

Run this server before executing the test suite:
    python server.py

Or with custom ports:
    HTTP_PORT=8080 WS_PORT=9000 python server.py
"""

import asyncio
import os
import sys

# Add parent directory to path to import pykrozen
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pykrozen import (
    Request,
    Response,
    ResponseDict,
    Server,
    UploadedFile,
    WSContext,
    WSMessage,
    get,
    html,
    on,
    post,
    text,
    upload,
    use,
)

# ──────────────────────────────────────────────────────────────────────────────
# Middleware
# ──────────────────────────────────────────────────────────────────────────────


@use
def logging_middleware(req: Request, res: Response) -> None:
    """Simple request logging middleware."""
    pass  # Silent for stress testing


@use
def cors_middleware(req: Request, res: Response) -> None:
    """Add CORS headers to all responses."""
    res.headers["Access-Control-Allow-Origin"] = "*"
    res.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    res.headers["Access-Control-Allow-Headers"] = "Content-Type"


# ──────────────────────────────────────────────────────────────────────────────
# GET Endpoints
# ──────────────────────────────────────────────────────────────────────────────


@get("/")
def index(req: Request) -> ResponseDict:
    """Root endpoint."""
    return {
        "body": {
            "message": "Welcome to PyKrozen Test Server",
            "version": "0.1.0",
            "endpoints": {
                "GET": ["/", "/health", "/echo", "/data", "/slow"],
                "POST": ["/", "/echo", "/data", "/upload"],
                "WebSocket": ["ws://localhost:8765"],
            },
        }
    }


@get("/health")
def health_check(req: Request) -> ResponseDict:
    """Health check endpoint."""
    return {
        "body": {"status": "healthy", "server": "pykrozen"},
        "status": 200,
    }


@get("/echo")
def echo_get(req: Request) -> ResponseDict:
    """Echo back query parameters."""
    return {
        "body": {
            "method": "GET",
            "path": req.path,
            "query": req.query,
            "headers": dict(req.headers),
        }
    }


@get("/data")
def get_data(req: Request) -> ResponseDict:
    """Return sample data."""
    return {
        "body": {
            "items": [
                {"id": 1, "name": "Item 1", "value": 100},
                {"id": 2, "name": "Item 2", "value": 200},
                {"id": 3, "name": "Item 3", "value": 300},
            ],
            "total": 3,
        }
    }


@get("/slow")
def slow_endpoint(req: Request) -> ResponseDict:
    """Intentionally slow endpoint for timeout testing."""
    import time

    delay = float(req.query.get("delay", "0.1"))
    time.sleep(min(delay, 5.0))  # Cap at 5 seconds
    return {"body": {"delayed": delay}}


# ──────────────────────────────────────────────────────────────────────────────
# Async GET Endpoints
# ──────────────────────────────────────────────────────────────────────────────


@get("/async")
async def async_endpoint(req: Request) -> ResponseDict:
    """Async endpoint example."""
    await asyncio.sleep(0.01)  # Simulate async I/O
    return {
        "body": {"async": True, "message": "This was handled asynchronously!"},
        "status": 200,
    }


@get("/async/slow")
async def async_slow_endpoint(req: Request) -> ResponseDict:
    """Async slow endpoint using non-blocking sleep."""
    delay = float(req.query.get("delay", "0.1"))
    await asyncio.sleep(min(delay, 5.0))  # Non-blocking sleep
    return {"body": {"async": True, "delayed": delay}}


@get("/html")
def html_page(req: Request) -> ResponseDict:
    """Return HTML content."""
    return html(
        """
        <!DOCTYPE html>
        <html>
        <head><title>PyKrozen Test</title></head>
        <body>
            <h1>PyKrozen Test Server</h1>
            <p>This is an HTML response.</p>
        </body>
        </html>
        """
    )


@get("/text")
def text_response(req: Request) -> ResponseDict:
    """Return plain text."""
    return text("This is plain text response from PyKrozen server.")


@get("/large")
def large_response(req: Request) -> ResponseDict:
    """Return a large JSON response."""
    size = int(req.query.get("size", "1000"))
    size = min(size, 100000)  # Cap at 100KB worth of items
    return {
        "body": {
            "items": [{"id": i, "data": f"item_{i}" * 10} for i in range(size)],
            "count": size,
        }
    }


# ──────────────────────────────────────────────────────────────────────────────
# POST Endpoints
# ──────────────────────────────────────────────────────────────────────────────


@post("/")
def post_root(req: Request) -> ResponseDict:
    """Accept POST to root."""
    return {
        "body": {
            "received": req.body,
            "message": "POST request received",
        },
        "status": 201,
    }


@post("/echo")
def echo_post(req: Request) -> ResponseDict:
    """Echo back the POST body."""
    return {
        "body": {
            "method": "POST",
            "path": req.path,
            "body": req.body,
            "content_type": req.headers.get("Content-Type", "unknown"),
        }
    }


@post("/data")
def post_data(req: Request) -> ResponseDict:
    """Accept and validate data."""
    body = req.body if isinstance(req.body, dict) else {}

    if not body:
        return {
            "body": {"error": "Request body required"},
            "status": 400,
        }

    return {
        "body": {
            "success": True,
            "received": body,
            "keys": list(body.keys()) if isinstance(body, dict) else [],
        },
        "status": 201,
    }


@post("/validate")
def validate_data(req: Request) -> ResponseDict:
    """Validate POST data structure."""
    body = req.body if isinstance(req.body, dict) else {}

    required_fields = ["name", "value"]
    missing = [f for f in required_fields if f not in body]

    if missing:
        return {
            "body": {"error": "Missing required fields", "missing": missing},
            "status": 400,
        }

    return {
        "body": {"valid": True, "data": body},
        "status": 200,
    }


# File upload handler
def handle_upload(files: list[UploadedFile]) -> ResponseDict:
    """Handle file uploads."""
    uploaded = []
    for f in files:
        uploaded.append(
            {
                "filename": f["filename"],
                "content_type": f["content_type"],
                "size": len(f["content"]),
            }
        )

    return {
        "body": {"uploaded": len(files), "files": uploaded},
        "status": 201,
    }


upload("/upload", handle_upload)


# ──────────────────────────────────────────────────────────────────────────────
# WebSocket Events
# ──────────────────────────────────────────────────────────────────────────────


@on("ws:connect")
def ws_connect(ctx: WSContext) -> None:
    """Handle WebSocket connection."""
    ctx.ws.data.message_count = 0


@on("ws:message")
def ws_message(ctx: WSMessage) -> None:
    """Handle WebSocket messages."""
    ctx.ws.data.message_count += 1

    # Handle different message types
    data = ctx.data if isinstance(ctx.data, dict) else {"raw": ctx.data}
    msg_type = data.get("type", "echo")

    if msg_type == "ping":
        ctx.reply = {"type": "pong", "timestamp": data.get("timestamp")}
        ctx.stop = True
    elif msg_type == "count":
        ctx.reply = {
            "type": "count",
            "messages_received": ctx.ws.data.message_count,
        }
        ctx.stop = True
    elif msg_type == "broadcast":
        # In a real app, you'd broadcast to all clients
        ctx.reply = {"type": "broadcast_ack", "message": data.get("message")}
        ctx.stop = True
    # Default: echo behavior (handled by pykrozen)


@on("ws:disconnect")
def ws_disconnect(ctx: WSContext) -> None:
    """Handle WebSocket disconnection."""
    pass


# ──────────────────────────────────────────────────────────────────────────────
# Server Hooks
# ──────────────────────────────────────────────────────────────────────────────


@on("server:start")
def on_start(ctx) -> None:
    """Called when server starts."""
    print("\n" + "=" * 50)
    print("  PyKrozen Test Server Started")
    print("=" * 50)


@on("server:stop")
def on_stop(ctx) -> None:
    """Called when server stops."""
    print("\n" + "=" * 50)
    print("  PyKrozen Test Server Stopped")
    print("=" * 50)


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    http_port = int(os.environ.get("HTTP_PORT", "8000"))
    ws_port = int(os.environ.get("WS_PORT", "8765"))
    host = os.environ.get("HOST", "127.0.0.1")

    print(f"\nStarting server on {host}")
    print(f"  HTTP: http://{host}:{http_port}")
    print(f"  WebSocket: ws://{host}:{ws_port}")
    print("\nPress Ctrl+C to stop.\n")

    Server.run()
