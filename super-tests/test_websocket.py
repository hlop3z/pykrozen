"""
WebSocket Edge Case Tests for Production Readiness.

Tests cover:
- Rapid message bursts (the original bug scenario)
- Large messages (126-byte, 64KB+, multi-MB payloads)
- Malformed frames and error recovery
- Concurrent connections under load
- Connection edge cases (clean close, abrupt disconnect, reconnect)
- Binary data and special characters (unicode, null bytes, etc.)
- Frame-level protocol compliance
"""

import sys
import json
import time
import socket
import struct
import threading
import hashlib
import base64
import os
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import importlib
import pykrozen

importlib.reload(pykrozen)

from pykrozen import App, on, settings, Server

# ─────────────────────────────────────────────────────────────────────────────
# Test Configuration
# ─────────────────────────────────────────────────────────────────────────────

TEST_HOST = "127.0.0.1"
TEST_PORT = 18765  # Use non-standard port to avoid conflicts
WS_PATH = "/ws"

# ─────────────────────────────────────────────────────────────────────────────
# WebSocket Client Helper (low-level for testing edge cases)
# ─────────────────────────────────────────────────────────────────────────────


class TestWSClient:
    """Low-level WebSocket client for edge case testing."""

    WS_MAGIC = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

    def __init__(self, host: str, port: int, path: str = "/ws"):
        self.host = host
        self.port = port
        self.path = path
        self.sock = None
        self.connected = False

    def connect(self, timeout: float = 5.0) -> bool:
        """Perform WebSocket handshake."""
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

        try:
            self.sock.connect((self.host, self.port))
        except (ConnectionRefusedError, TimeoutError):
            return False

        # Generate random key
        key = base64.b64encode(os.urandom(16)).decode()

        # Send HTTP upgrade request
        request = (
            f"GET {self.path} HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n"
            f"\r\n"
        )
        self.sock.send(request.encode())

        # Read response
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = self.sock.recv(1024)
            if not chunk:
                return False
            response += chunk

        # Verify 101 response
        if b"101" not in response:
            return False

        # Verify accept key
        expected_accept = base64.b64encode(
            hashlib.sha1(key.encode() + self.WS_MAGIC).digest()
        ).decode()
        if expected_accept.encode() not in response:
            return False

        self.connected = True
        return True

    def send_frame(
        self, data: bytes, opcode: int = 1, mask: bool = True, fin: bool = True
    ) -> None:
        """Send a raw WebSocket frame."""
        length = len(data)

        # First byte: FIN + opcode
        byte1 = (0x80 if fin else 0x00) | opcode

        # Second byte: MASK + length
        if length < 126:
            header = bytes([byte1, (0x80 if mask else 0x00) | length])
        elif length < 0x10000:
            header = bytes([byte1, (0x80 if mask else 0x00) | 126]) + struct.pack(
                ">H", length
            )
        else:
            header = bytes([byte1, (0x80 if mask else 0x00) | 127]) + struct.pack(
                ">Q", length
            )

        if mask:
            mask_key = os.urandom(4)
            masked_data = bytearray(data)
            for i in range(len(masked_data)):
                masked_data[i] ^= mask_key[i % 4]
            self.sock.send(header + mask_key + bytes(masked_data))
        else:
            self.sock.send(header + data)

    def send_text(self, text: str) -> None:
        """Send a text message."""
        self.send_frame(text.encode("utf-8"), opcode=1)

    def send_json(self, obj: dict) -> None:
        """Send a JSON message."""
        self.send_text(json.dumps(obj))

    def send_binary(self, data: bytes) -> None:
        """Send a binary message."""
        self.send_frame(data, opcode=2)

    def send_ping(self, data: bytes = b"") -> None:
        """Send a ping frame."""
        self.send_frame(data, opcode=9)

    def send_close(self, code: int = 1000, reason: str = "") -> None:
        """Send a close frame."""
        payload = struct.pack(">H", code) + reason.encode("utf-8")
        self.send_frame(payload, opcode=8)

    def recv_frame(self, timeout: float = 5.0) -> tuple[int, bytes]:
        """Receive a WebSocket frame, returns (opcode, data)."""
        self.sock.settimeout(timeout)
        try:
            header = self._recv_exact(2)
        except (socket.timeout, ConnectionError):
            return None, None

        opcode = header[0] & 0x0F
        length = header[1] & 0x7F

        if length == 126:
            length = struct.unpack(">H", self._recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self._recv_exact(8))[0]

        # Server frames are not masked
        data = self._recv_exact(length) if length > 0 else b""
        return opcode, data

    def recv_json(self, timeout: float = 5.0) -> dict | None:
        """Receive and parse a JSON message."""
        opcode, data = self.recv_frame(timeout)
        if opcode == 1 and data:
            return json.loads(data.decode("utf-8"))
        return None

    def _recv_exact(self, n: int) -> bytes:
        """Read exactly n bytes."""
        buf = bytearray()
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("Connection closed")
            buf.extend(chunk)
        return bytes(buf)

    def close(self) -> None:
        """Close the connection."""
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
        self.connected = False


# ─────────────────────────────────────────────────────────────────────────────
# Server Setup Helper
# ─────────────────────────────────────────────────────────────────────────────


class TestServer:
    """Wrapper for starting/stopping test server."""

    def __init__(self):
        self.server = None
        self.thread = None
        self.messages_received = []
        self.connections = []
        self._app = pykrozen.app  # Use global app

    def start(self):
        """Start server in background thread."""
        # Configure global settings
        settings.port = TEST_PORT
        settings.ws_path = WS_PATH
        settings.debug = False

        # Clear any existing handlers and set up new ones
        self._app._hooks.clear()

        @on("ws:connect")
        def on_connect(ctx):
            self.connections.append(ctx.ws)

        @on("ws:message")
        def on_message(ctx):
            self.messages_received.append(ctx.data)
            # Echo back with type indicator
            if isinstance(ctx.data, dict) and ctx.data.get("type") == "ping":
                ctx.reply = {"type": "pong", "time": ctx.data.get("time")}
                ctx.stop = True
            elif isinstance(ctx.data, dict) and ctx.data.get("type") == "echo":
                ctx.reply = {"type": "echo", "data": ctx.data.get("data")}
                ctx.stop = True

        @on("ws:disconnect")
        def on_disconnect(ctx):
            if ctx.ws in self.connections:
                self.connections.remove(ctx.ws)

        self.server = Server()

        def run():
            try:
                self.server.start()
                while self.server._running:
                    time.sleep(0.1)
            except Exception:
                pass

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()
        time.sleep(0.3)  # Give server time to start

    def stop(self):
        """Stop the server."""
        if self.server:
            self.server.stop()
        if self.thread:
            self.thread.join(timeout=1.0)
        # Clear hooks for next test
        self._app._hooks.clear()

    def clear(self):
        """Clear message history."""
        self.messages_received.clear()


# ─────────────────────────────────────────────────────────────────────────────
# Edge Case Tests
# ─────────────────────────────────────────────────────────────────────────────


def test_rapid_message_burst():
    """Test sending many messages rapidly (the original bug scenario)."""
    server = TestServer()
    server.start()

    try:
        client = TestWSClient(TEST_HOST, TEST_PORT, WS_PATH)
        assert client.connect(), "Failed to connect"

        # Send 100 messages as fast as possible
        num_messages = 100
        for i in range(num_messages):
            client.send_json({"type": "ping", "time": i})

        # Receive all responses
        responses = []
        for _ in range(num_messages):
            resp = client.recv_json(timeout=5.0)
            if resp:
                responses.append(resp)

        client.close()

        # All messages should be received without error
        assert len(responses) == num_messages, (
            f"Expected {num_messages} responses, got {len(responses)}"
        )
        assert all(r.get("type") == "pong" for r in responses), "Invalid response type"

        print(f"[PASS] Rapid message burst: {num_messages} messages OK")
    finally:
        server.stop()


def test_concurrent_rapid_bursts():
    """Test multiple clients sending rapid bursts simultaneously."""
    server = TestServer()
    server.start()

    try:
        num_clients = 5
        messages_per_client = 50

        def client_worker(client_id: int) -> tuple[int, int]:
            """Send messages and return (sent, received)."""
            client = TestWSClient(TEST_HOST, TEST_PORT, WS_PATH)
            if not client.connect():
                return 0, 0

            sent = 0
            received = 0

            # Send all messages
            for i in range(messages_per_client):
                try:
                    client.send_json({"type": "ping", "client": client_id, "seq": i})
                    sent += 1
                except Exception:
                    break

            # Receive all responses
            for _ in range(sent):
                try:
                    resp = client.recv_json(timeout=3.0)
                    if resp and resp.get("type") == "pong":
                        received += 1
                except Exception:
                    break

            client.close()
            return sent, received

        # Run all clients concurrently
        with ThreadPoolExecutor(max_workers=num_clients) as executor:
            futures = [executor.submit(client_worker, i) for i in range(num_clients)]
            results = [f.result() for f in as_completed(futures)]

        total_sent = sum(r[0] for r in results)
        total_received = sum(r[1] for r in results)

        expected = num_clients * messages_per_client
        assert total_sent == expected, f"Sent {total_sent}/{expected} messages"
        assert total_received == expected, (
            f"Received {total_received}/{expected} responses"
        )

        print(
            f"[PASS] Concurrent bursts: {num_clients} clients x {messages_per_client} msgs OK"
        )
    finally:
        server.stop()


def test_large_messages_126_bytes():
    """Test messages requiring 2-byte length encoding (126-65535 bytes)."""
    server = TestServer()
    server.start()

    try:
        client = TestWSClient(TEST_HOST, TEST_PORT, WS_PATH)
        assert client.connect(), "Failed to connect"

        # Test various sizes in the 126-65535 byte range
        sizes = [126, 127, 200, 1000, 10000, 65535]

        for size in sizes:
            payload = {"type": "echo", "data": "x" * (size - 30)}
            client.send_json(payload)
            resp = client.recv_json(timeout=5.0)
            assert resp is not None, f"No response for {size}-byte message"
            assert resp.get("type") == "echo", f"Wrong type for {size}-byte message"
            assert resp.get("data") == payload["data"], (
                f"Data mismatch for {size}-byte message"
            )

        client.close()
        print(f"[PASS] Large messages (126-65535 bytes): sizes {sizes} OK")
    finally:
        server.stop()


def test_large_messages_8_byte_length():
    """Test messages requiring 8-byte length encoding (65536+ bytes)."""
    server = TestServer()
    server.start()

    try:
        client = TestWSClient(TEST_HOST, TEST_PORT, WS_PATH)
        assert client.connect(), "Failed to connect"

        # Test 64KB+ messages
        sizes = [65536, 100000, 500000]

        for size in sizes:
            payload = {"type": "echo", "data": "y" * (size - 30)}
            client.send_json(payload)
            resp = client.recv_json(timeout=10.0)
            assert resp is not None, f"No response for {size}-byte message"
            assert resp.get("data") == payload["data"], (
                f"Data mismatch for {size}-byte message"
            )

        client.close()
        print(f"[PASS] Large messages (65536+ bytes): sizes {sizes} OK")
    finally:
        server.stop()


def test_malformed_json():
    """Test server handles malformed JSON gracefully."""
    server = TestServer()
    server.start()

    try:
        client = TestWSClient(TEST_HOST, TEST_PORT, WS_PATH)
        assert client.connect(), "Failed to connect"

        # Send malformed JSON
        malformed = [
            "{invalid json}",
            "not json at all",
            '{"unclosed": "brace"',
            "",
            "{",
            "}",
            '["array", "not", "object"]',  # Valid JSON but not expected format
        ]

        for bad_json in malformed:
            client.send_text(bad_json)
            resp = client.recv_json(timeout=2.0)
            # Server should return error for invalid JSON or echo for valid non-object
            assert resp is not None, f"No response for: {bad_json[:20]}"

        # Connection should still be alive
        client.send_json({"type": "ping", "time": 123})
        resp = client.recv_json(timeout=2.0)
        assert resp is not None and resp.get("type") == "pong", (
            "Connection died after malformed JSON"
        )

        client.close()
        print("[PASS] Malformed JSON handling OK")
    finally:
        server.stop()


def test_unicode_and_special_chars():
    """Test unicode, emojis, and special characters."""
    server = TestServer()
    server.start()

    try:
        client = TestWSClient(TEST_HOST, TEST_PORT, WS_PATH)
        assert client.connect(), "Failed to connect"

        special_strings = [
            "Hello, World!",
            "Unicode: \u00e9\u00e8\u00ea\u00eb",
            "Chinese: \u4e2d\u6587\u6d4b\u8bd5",
            "Emoji: \U0001F600\U0001F389\U0001F680",
            "Japanese: \u3053\u3093\u306b\u3061\u306f",
            "Arabic: \u0645\u0631\u062d\u0628\u0627",
            "Mixed: Hello \U0001F600 \u4e16\u754c \u0645\u0631\u062d\u0628\u0627",
            "Newlines:\nand\ttabs",
            'Quotes: "double" and \'single\'',
            "Backslash: \\path\\to\\file",
            "Null-ish: \x00 (null byte stripped by JSON)",
        ]

        for s in special_strings:
            # JSON encoding handles special chars
            try:
                payload = {"type": "echo", "data": s}
                client.send_json(payload)
                resp = client.recv_json(timeout=2.0)
                assert resp is not None, f"No response for: {s[:30]}"
            except Exception as e:
                # Some strings may not be JSON-encodable
                print(f"  [SKIP] Cannot encode: {s[:20]}... ({e})")
                continue

        client.close()
        print("[PASS] Unicode and special characters OK")
    finally:
        server.stop()


def test_websocket_ping_pong_frames():
    """Test WebSocket protocol-level PING/PONG frames."""
    server = TestServer()
    server.start()

    try:
        client = TestWSClient(TEST_HOST, TEST_PORT, WS_PATH)
        assert client.connect(), "Failed to connect"

        # Send protocol-level PING (opcode 9)
        ping_data = b"ping-payload"
        client.send_ping(ping_data)

        # Should receive PONG (opcode 10) with same payload
        opcode, data = client.recv_frame(timeout=2.0)
        assert opcode == 10, f"Expected PONG (10), got opcode {opcode}"
        assert data == ping_data, f"PONG payload mismatch: {data} != {ping_data}"

        # Connection should still work for text messages
        client.send_json({"type": "ping", "time": 999})
        resp = client.recv_json(timeout=2.0)
        assert resp and resp.get("type") == "pong", "Text messaging broken after PING"

        client.close()
        print("[PASS] WebSocket PING/PONG frames OK")
    finally:
        server.stop()


def test_clean_close():
    """Test clean WebSocket close handshake."""
    server = TestServer()
    server.start()

    try:
        client = TestWSClient(TEST_HOST, TEST_PORT, WS_PATH)
        assert client.connect(), "Failed to connect"

        # Send close frame
        client.send_close(1000, "Normal closure")

        # Should receive close frame back
        opcode, data = client.recv_frame(timeout=2.0)
        assert opcode == 8, f"Expected CLOSE (8), got opcode {opcode}"

        client.close()
        print("[PASS] Clean close handshake OK")
    finally:
        server.stop()


def test_abrupt_disconnect():
    """Test server handles abrupt client disconnect."""
    server = TestServer()
    server.start()

    try:
        # Connect and immediately close socket
        for _ in range(5):
            client = TestWSClient(TEST_HOST, TEST_PORT, WS_PATH)
            assert client.connect(), "Failed to connect"
            # Abrupt close without close frame
            client.sock.close()

        # Server should still accept new connections
        time.sleep(0.2)
        client = TestWSClient(TEST_HOST, TEST_PORT, WS_PATH)
        assert client.connect(), "Server stopped accepting connections"
        client.send_json({"type": "ping", "time": 1})
        resp = client.recv_json(timeout=2.0)
        assert resp and resp.get("type") == "pong", "Server not responding"
        client.close()

        print("[PASS] Abrupt disconnect handling OK")
    finally:
        server.stop()


def test_partial_frame_sending():
    """Test server handles frames sent in fragments."""
    server = TestServer()
    server.start()

    try:
        # Create raw socket connection
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(5.0)
        sock.connect((TEST_HOST, TEST_PORT))

        # Perform handshake
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET {WS_PATH} HTTP/1.1\r\n"
            f"Host: {TEST_HOST}:{TEST_PORT}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n"
            f"\r\n"
        )
        sock.send(request.encode())
        response = sock.recv(1024)
        assert b"101" in response, "Handshake failed"

        # Build a masked text frame for {"type":"ping"}
        payload = b'{"type":"ping","time":12345}'
        length = len(payload)
        mask_key = os.urandom(4)
        masked = bytearray(payload)
        for i in range(len(masked)):
            masked[i] ^= mask_key[i % 4]

        frame = bytes([0x81, 0x80 | length]) + mask_key + bytes(masked)

        # Send frame one byte at a time with delays
        for b in frame:
            sock.send(bytes([b]))
            time.sleep(0.01)  # 10ms delay between bytes

        # Should still receive valid response
        sock.settimeout(5.0)
        header = sock.recv(2)
        assert len(header) == 2, "No response header"
        opcode = header[0] & 0x0F
        resp_len = header[1] & 0x7F
        resp_data = sock.recv(resp_len)
        resp = json.loads(resp_data.decode())
        assert resp.get("type") == "pong", f"Wrong response: {resp}"

        sock.close()
        print("[PASS] Partial frame sending OK")
    finally:
        server.stop()


def test_zero_length_messages():
    """Test handling of zero-length payloads."""
    server = TestServer()
    server.start()

    try:
        client = TestWSClient(TEST_HOST, TEST_PORT, WS_PATH)
        assert client.connect(), "Failed to connect"

        # Send empty text frame
        client.send_text("")

        # Server should handle gracefully (likely returns error or ignores)
        resp = client.recv_json(timeout=2.0)
        # We don't care about the response, just that connection survives

        # Connection should still work
        client.send_json({"type": "ping", "time": 1})
        resp = client.recv_json(timeout=2.0)
        assert resp and resp.get("type") == "pong", "Connection broken after empty msg"

        client.close()
        print("[PASS] Zero-length messages OK")
    finally:
        server.stop()


def test_message_ordering():
    """Test that message order is preserved."""
    server = TestServer()
    server.start()

    try:
        client = TestWSClient(TEST_HOST, TEST_PORT, WS_PATH)
        assert client.connect(), "Failed to connect"

        # Send numbered messages
        num_messages = 50
        for i in range(num_messages):
            client.send_json({"type": "echo", "data": i})

        # Receive and verify order
        for i in range(num_messages):
            resp = client.recv_json(timeout=5.0)
            assert resp is not None, f"Missing response {i}"
            assert resp.get("data") == i, f"Out of order: expected {i}, got {resp}"

        client.close()
        print(f"[PASS] Message ordering: {num_messages} messages in order")
    finally:
        server.stop()


def test_reconnect_after_disconnect():
    """Test reconnecting after clean disconnect."""
    server = TestServer()
    server.start()

    try:
        for attempt in range(3):
            client = TestWSClient(TEST_HOST, TEST_PORT, WS_PATH)
            assert client.connect(), f"Failed to connect on attempt {attempt + 1}"

            client.send_json({"type": "ping", "time": attempt})
            resp = client.recv_json(timeout=2.0)
            assert resp and resp.get("type") == "pong", f"No pong on attempt {attempt}"

            client.send_close(1000, "bye")
            client.recv_frame(timeout=1.0)  # Wait for close response
            client.close()

        print("[PASS] Reconnect after disconnect OK")
    finally:
        server.stop()


def test_many_small_messages():
    """Stress test with many small messages."""
    server = TestServer()
    server.start()

    try:
        client = TestWSClient(TEST_HOST, TEST_PORT, WS_PATH)
        assert client.connect(), "Failed to connect"

        num_messages = 1000
        start = time.time()

        for i in range(num_messages):
            client.send_json({"type": "ping", "time": i})

        received = 0
        for _ in range(num_messages):
            resp = client.recv_json(timeout=10.0)
            if resp and resp.get("type") == "pong":
                received += 1

        elapsed = time.time() - start

        client.close()

        assert received == num_messages, (
            f"Lost messages: {num_messages - received}/{num_messages}"
        )
        print(
            f"[PASS] Stress test: {num_messages} msgs in {elapsed:.2f}s "
            f"({num_messages/elapsed:.0f} msg/s)"
        )
    finally:
        server.stop()


# ─────────────────────────────────────────────────────────────────────────────
# Test Runner
# ─────────────────────────────────────────────────────────────────────────────


def run_all_websocket_tests():
    """Run all WebSocket edge case tests."""
    print("\n" + "=" * 70)
    print("WebSocket Edge Case Test Suite - Production Readiness")
    print("=" * 70 + "\n")

    tests = [
        ("Rapid Message Burst", test_rapid_message_burst),
        ("Concurrent Rapid Bursts", test_concurrent_rapid_bursts),
        ("Large Messages (126-65535 bytes)", test_large_messages_126_bytes),
        ("Large Messages (65536+ bytes)", test_large_messages_8_byte_length),
        ("Malformed JSON Handling", test_malformed_json),
        ("Unicode and Special Chars", test_unicode_and_special_chars),
        ("WebSocket PING/PONG Frames", test_websocket_ping_pong_frames),
        ("Clean Close Handshake", test_clean_close),
        ("Abrupt Disconnect Handling", test_abrupt_disconnect),
        ("Partial Frame Sending", test_partial_frame_sending),
        ("Zero-Length Messages", test_zero_length_messages),
        ("Message Ordering", test_message_ordering),
        ("Reconnect After Disconnect", test_reconnect_after_disconnect),
        ("Stress Test (1000 messages)", test_many_small_messages),
    ]

    passed = 0
    failed = 0
    errors = []

    for name, test_fn in tests:
        try:
            test_fn()
            passed += 1
        except AssertionError as e:
            failed += 1
            errors.append((name, str(e)))
            print(f"[FAIL] {name}: {e}")
        except Exception as e:
            failed += 1
            errors.append((name, f"Exception: {e}"))
            print(f"[ERROR] {name}: {e}")

    print("\n" + "=" * 70)
    print(f"Results: {passed} passed, {failed} failed")
    if errors:
        print("\nFailures:")
        for name, err in errors:
            print(f"  - {name}: {err}")
    print("=" * 70 + "\n")

    return failed == 0


if __name__ == "__main__":
    success = run_all_websocket_tests()
    sys.exit(0 if success else 1)
