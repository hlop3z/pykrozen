"""WebSocket client and message handling."""

from __future__ import annotations

import asyncio
import socket
import struct
import threading
from base64 import b64encode
from dataclasses import dataclass, field
from hashlib import sha1
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, NamedTuple

from pykrozen.constants import (
    ERROR_CONNECTION_CLOSED,
    HANDSHAKE_BUFFER_SIZE,
    WS_ACCEPT_PREFIX,
    WS_FINAL_FRAME_FLAG,
    WS_LENGTH_16BIT,
    WS_LENGTH_32BIT_MAX,
    WS_LENGTH_64BIT,
    WS_LENGTH_MASK,
    WS_MAGIC,
    WS_OPCODE_MASK,
    WS_UPGRADE_HEADER,
    WS_UPGRADE_RESPONSE,
    WS_WAIT_TIMEOUT,
    WSOpcode,
)
from pykrozen.utils import _json_dumps, _xor_unmask_fast

if TYPE_CHECKING:
    pass


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
            WS_UPGRADE_RESPONSE
            + WS_UPGRADE_HEADER
            + WS_ACCEPT_PREFIX
            + accept
            + b"\r\n\r\n"
        )

    def handshake(self) -> None:
        """Perform WebSocket handshake."""
        req = self.sock.recv(HANDSHAKE_BUFFER_SIZE)
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
                raise ConnectionError(ERROR_CONNECTION_CLOSED)
            buf.extend(chunk)
        return bytes(buf)

    def recv(self) -> tuple[int | None, bytes | None]:
        """Receive a WebSocket frame, returns (opcode, data)."""
        with self._recv_lock:
            try:
                header = self._recv_exact(2)
                opcode = header[0] & WS_OPCODE_MASK
                length = header[1] & WS_LENGTH_MASK

                if length == WS_LENGTH_16BIT:
                    length = struct.unpack(">H", self._recv_exact(2))[0]
                elif length == WS_LENGTH_64BIT:
                    length = struct.unpack(">Q", self._recv_exact(8))[0]

                if header[1] & WS_FINAL_FRAME_FLAG:
                    mask = self._recv_exact(4)
                    payload = self._recv_exact(length) if length > 0 else b""
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

                if length < WS_LENGTH_16BIT:
                    frame = bytes([WS_FINAL_FRAME_FLAG | opcode, length])
                    self.sock.sendall(frame + payload)
                elif length < WS_LENGTH_32BIT_MAX:
                    header = bytes([WS_FINAL_FRAME_FLAG | opcode, WS_LENGTH_16BIT])
                    self.sock.sendall(header + struct.pack(">H", length) + payload)
                else:
                    header = bytes([WS_FINAL_FRAME_FLAG | opcode, WS_LENGTH_64BIT])
                    self.sock.sendall(header + struct.pack(">Q", length) + payload)
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
            try:
                await asyncio.wait_for(self._data_event.wait(), timeout=WS_WAIT_TIMEOUT)
            except TimeoutError:
                continue
        return True

    async def recv_async(self) -> tuple[int | None, bytes | None]:
        """Receive a WebSocket frame asynchronously (event-driven, no polling)."""
        try:
            if not await self._wait_for_bytes(2):
                return None, None

            header = bytes(self._buffer[:2])
            opcode = header[0] & WS_OPCODE_MASK
            length = header[1] & WS_LENGTH_MASK
            offset = 2

            if length == WS_LENGTH_16BIT:
                if not await self._wait_for_bytes(offset + 2):
                    return None, None
                length = struct.unpack(">H", bytes(self._buffer[offset : offset + 2]))[
                    0
                ]
                offset += 2
            elif length == WS_LENGTH_64BIT:
                if not await self._wait_for_bytes(offset + 8):
                    return None, None
                length = struct.unpack(">Q", bytes(self._buffer[offset : offset + 8]))[
                    0
                ]
                offset += 8

            masked = header[1] & WS_FINAL_FRAME_FLAG
            mask = b""
            if masked:
                if not await self._wait_for_bytes(offset + 4):
                    return None, None
                mask = bytes(self._buffer[offset : offset + 4])
                offset += 4

            if not await self._wait_for_bytes(offset + length):
                return None, None

            payload = bytes(self._buffer[offset : offset + length])
            del self._buffer[: offset + length]

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
        self._data_event.set()

    def send(self, data: Any, opcode: int = WSOpcode.TEXT) -> bool:
        """Send data over WebSocket."""
        try:
            if not self.transport or self.transport.is_closing():
                return False

            if opcode == WSOpcode.TEXT and not isinstance(data, (str, bytes)):
                data = _json_dumps(data)
            payload = data.encode() if isinstance(data, str) else data
            length = len(payload)

            if length < WS_LENGTH_16BIT:
                frame = bytes([WS_FINAL_FRAME_FLAG | opcode, length])
                self.transport.write(frame + payload)
            elif length < WS_LENGTH_32BIT_MAX:
                header = bytes([WS_FINAL_FRAME_FLAG | opcode, WS_LENGTH_16BIT])
                self.transport.write(header + struct.pack(">H", length) + payload)
            else:
                header = bytes([WS_FINAL_FRAME_FLAG | opcode, WS_LENGTH_64BIT])
                self.transport.write(header + struct.pack(">Q", length) + payload)
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
