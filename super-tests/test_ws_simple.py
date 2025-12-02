"""Simple WebSocket test to verify the fix works."""

import sys
import json
import time
import socket
import struct
import os
from pathlib import Path
import hashlib
import base64

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


class SimpleWSClient:
    """Minimal WebSocket client for testing."""

    WS_MAGIC = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

    def __init__(self, host: str, port: int, path: str = "/ws"):
        self.host = host
        self.port = port
        self.path = path
        self.sock = None

    def connect(self, timeout: float = 5.0) -> bool:
        """Perform WebSocket handshake."""
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

        try:
            self.sock.connect((self.host, self.port))
        except Exception as e:
            print(f"Connection failed: {e}")
            return False

        key = base64.b64encode(os.urandom(16)).decode()
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

        response = b""
        while b"\r\n\r\n" not in response:
            chunk = self.sock.recv(1024)
            if not chunk:
                return False
            response += chunk

        if b"101" not in response:
            print(f"Bad response: {response[:100]}")
            return False

        return True

    def send_json(self, obj: dict) -> None:
        """Send a JSON message."""
        data = json.dumps(obj).encode("utf-8")
        length = len(data)
        mask_key = os.urandom(4)

        if length < 126:
            header = bytes([0x81, 0x80 | length])
        elif length < 0x10000:
            header = bytes([0x81, 0x80 | 126]) + struct.pack(">H", length)
        else:
            header = bytes([0x81, 0x80 | 127]) + struct.pack(">Q", length)

        masked = bytearray(data)
        for i in range(len(masked)):
            masked[i] ^= mask_key[i % 4]

        self.sock.send(header + mask_key + bytes(masked))

    def recv_json(self, timeout: float = 5.0) -> dict | None:
        """Receive a JSON message."""
        self.sock.settimeout(timeout)
        try:
            header = self._recv_exact(2)
            if not header:
                return None

            opcode = header[0] & 0x0F
            length = header[1] & 0x7F

            if length == 126:
                length = struct.unpack(">H", self._recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._recv_exact(8))[0]

            data = self._recv_exact(length) if length > 0 else b""
            if opcode == 1 and data:
                return json.loads(data.decode("utf-8"))
            return None
        except Exception as e:
            print(f"Recv error: {e}")
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
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass


def test_rapid_messages():
    """Test rapid message sending - the original bug scenario."""
    print("\n=== Testing Rapid Messages (Original Bug Scenario) ===\n")

    client = SimpleWSClient("127.0.0.1", 8000, "/ws")

    if not client.connect():
        print("FAIL: Could not connect to server")
        print("Make sure the server is running: py super-tests/server.py")
        return False

    print("Connected to WebSocket server")

    # Send 50 rapid messages
    num_messages = 50
    print(f"Sending {num_messages} rapid ping messages...")

    for i in range(num_messages):
        client.send_json({"type": "ping", "timestamp": i})

    print("Receiving responses...")
    received = 0
    errors = 0

    for i in range(num_messages):
        resp = client.recv_json(timeout=5.0)
        if resp:
            if resp.get("type") == "pong":
                received += 1
            elif resp.get("error"):
                errors += 1
                print(f"  Error response: {resp}")
        else:
            print(f"  No response for message {i}")

    client.close()

    print(f"\nResults: {received}/{num_messages} successful, {errors} errors")

    if received == num_messages and errors == 0:
        print("PASS: All messages handled correctly!")
        return True
    else:
        print("FAIL: Some messages failed")
        return False


def test_large_message():
    """Test a large message (126+ bytes)."""
    print("\n=== Testing Large Message (126+ bytes) ===\n")

    client = SimpleWSClient("127.0.0.1", 8000, "/ws")

    if not client.connect():
        print("FAIL: Could not connect to server")
        return False

    # Send a message larger than 125 bytes
    large_data = "x" * 200
    client.send_json({"type": "ping", "data": large_data})

    resp = client.recv_json(timeout=5.0)
    client.close()

    if resp and resp.get("type") == "pong":
        print("PASS: Large message handled correctly!")
        return True
    else:
        print(f"FAIL: Response was {resp}")
        return False


if __name__ == "__main__":
    print("=" * 60)
    print("WebSocket Simple Test Suite")
    print("=" * 60)
    print("\nMake sure the server is running:")
    print("  py super-tests/server.py\n")

    results = []

    results.append(("Rapid Messages", test_rapid_messages()))
    results.append(("Large Message", test_large_message()))

    print("\n" + "=" * 60)
    print("Summary:")
    for name, passed in results:
        status = "PASS" if passed else "FAIL"
        print(f"  {name}: {status}")
    print("=" * 60)

    all_passed = all(r[1] for r in results)
    sys.exit(0 if all_passed else 1)
