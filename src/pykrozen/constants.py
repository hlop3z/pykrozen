"""Centralized constants for pykrozen."""

from enum import Enum

# ──────────────────────────────────────────────────────────────────────────────
# Router Node Types
# ──────────────────────────────────────────────────────────────────────────────

NODE_STATIC = 0
NODE_DYNAMIC = 1
NODE_WILDCARD = 2

# ──────────────────────────────────────────────────────────────────────────────
# HTTP Methods
# ──────────────────────────────────────────────────────────────────────────────


class HTTPMethod(str, Enum):
    """HTTP method constants."""

    GET = "GET"
    POST = "POST"
    PUT = "PUT"
    DELETE = "DELETE"
    PATCH = "PATCH"


# ──────────────────────────────────────────────────────────────────────────────
# WebSocket Opcodes
# ──────────────────────────────────────────────────────────────────────────────


class WSOpcode(int, Enum):
    """WebSocket frame opcodes."""

    TEXT = 1
    BINARY = 2
    CLOSE = 8
    PING = 9
    PONG = 10


# ──────────────────────────────────────────────────────────────────────────────
# WebSocket Frame Constants
# ──────────────────────────────────────────────────────────────────────────────

WS_OPCODE_MASK = 0x0F
WS_LENGTH_MASK = 0x7F
WS_FINAL_FRAME_FLAG = 0x80
WS_LENGTH_16BIT = 126
WS_LENGTH_64BIT = 127
WS_LENGTH_32BIT_MAX = 0x10000
WS_MAGIC = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

# ──────────────────────────────────────────────────────────────────────────────
# Buffer and Cache Sizes
# ──────────────────────────────────────────────────────────────────────────────

BUFFER_SIZE = 4096
BUFFER_POOL_SIZE = 32
HANDSHAKE_BUFFER_SIZE = 1024
SOCKET_BUFFER_SYNC = 32768
SOCKET_BUFFER_ASYNC = 65536

JSON_CACHE_MAX_SIZE = 2000
JSON_CACHE_MAX_ITEM_SIZE = 512
JSON_CACHE_MAX_DICT_KEYS = 5

# ──────────────────────────────────────────────────────────────────────────────
# JSON Serialization
# ──────────────────────────────────────────────────────────────────────────────

JSON_SEPARATORS: tuple[str, str] = (",", ":")

# ──────────────────────────────────────────────────────────────────────────────
# Encoding
# ──────────────────────────────────────────────────────────────────────────────

ENCODING_UTF8 = "utf-8"
CHARSET_UTF8_SUFFIX = "; charset=utf-8"

# ──────────────────────────────────────────────────────────────────────────────
# Event Hook Names
# ──────────────────────────────────────────────────────────────────────────────


class Hook(str, Enum):
    """Event hook names."""

    WS_CONNECT = "ws:connect"
    WS_MESSAGE = "ws:message"
    WS_DISCONNECT = "ws:disconnect"
    SERVER_START = "server:start"
    SERVER_STOP = "server:stop"
    HTTP_GET = "http:get"
    HTTP_POST = "http:post"
    HTTP_PUT = "http:put"
    HTTP_DELETE = "http:delete"
    HTTP_PATCH = "http:patch"


HOOK_AFTER_SUFFIX = ":after"

# ──────────────────────────────────────────────────────────────────────────────
# HTTP Headers
# ──────────────────────────────────────────────────────────────────────────────

HEADER_CONTENT_TYPE = "Content-Type"
HEADER_CONTENT_LENGTH = "Content-Length"
HEADER_CONNECTION = "Connection"
HEADER_CONTENT_DISPOSITION = "content-disposition"

# ──────────────────────────────────────────────────────────────────────────────
# Content Types
# ──────────────────────────────────────────────────────────────────────────────

CONTENT_TYPE_JSON = "application/json"
CONTENT_TYPE_HTML = "text/html; charset=utf-8"
CONTENT_TYPE_TEXT = "text/plain; charset=utf-8"
CONTENT_TYPE_OCTET = "application/octet-stream"
CONTENT_TYPE_MULTIPART = "multipart/form-data"

# ──────────────────────────────────────────────────────────────────────────────
# Multipart Form Data
# ──────────────────────────────────────────────────────────────────────────────

MULTIPART_FILENAME_MARKER = 'filename="'
MULTIPART_BOUNDARY_MARKER = "boundary="

# ──────────────────────────────────────────────────────────────────────────────
# WebSocket Handshake
# ──────────────────────────────────────────────────────────────────────────────

WS_UPGRADE_RESPONSE = b"HTTP/1.1 101 Switching Protocols\r\n"
WS_UPGRADE_HEADER = b"Upgrade: websocket\r\nConnection: Upgrade\r\n"
WS_ACCEPT_PREFIX = b"Sec-WebSocket-Accept: "

# ──────────────────────────────────────────────────────────────────────────────
# WebSocket Messages
# ──────────────────────────────────────────────────────────────────────────────

WS_ERROR_INVALID_JSON = "invalid json"
WS_DEFAULT_ECHO_KEY = "echo"

# ──────────────────────────────────────────────────────────────────────────────
# Error Messages
# ──────────────────────────────────────────────────────────────────────────────

ERROR_FILE_NOT_FOUND = "File not found"
ERROR_EXPECTED_MULTIPART = "Expected multipart/form-data"
ERROR_MISSING_BOUNDARY = "Missing boundary"
ERROR_INVALID_BODY = "Invalid body"
ERROR_CONNECTION_CLOSED = "Connection closed"

# ──────────────────────────────────────────────────────────────────────────────
# Timing Constants
# ──────────────────────────────────────────────────────────────────────────────

WS_WAIT_TIMEOUT = 0.1
SERVER_MAIN_THREAD_WAIT = 0.5

# ──────────────────────────────────────────────────────────────────────────────
# HTTP Status Codes
# ──────────────────────────────────────────────────────────────────────────────

HTTP_STATUS: dict[int, str] = {
    200: "OK",
    201: "Created",
    204: "No Content",
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
    500: "Internal Server Error",
}

# ──────────────────────────────────────────────────────────────────────────────
# MIME Types
# ──────────────────────────────────────────────────────────────────────────────

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

# ──────────────────────────────────────────────────────────────────────────────
# Pre-built HTTP Headers (for performance)
# ──────────────────────────────────────────────────────────────────────────────

PREBUILT_STATUS_HEADERS: dict[int, bytes] = {
    status: f"HTTP/1.1 {status} {text}\r\n".encode()
    for status, text in HTTP_STATUS.items()
}

HEADER_JSON_BYTES = b"Content-Type: application/json\r\n"
HEADER_KEEPALIVE_BYTES = b"Connection: keep-alive\r\n"
HEADER_CLOSE_BYTES = b"Connection: close\r\n"
