# PyKrozen Test Suite

A comprehensive Rust-based testing tool for the PyKrozen HTTP/WebSocket server library. Tests all endpoints, performs stress testing, and measures requests per second (RPS).

## Features

- **HTTP GET Testing**: Test GET endpoints with query parameters
- **HTTP POST Testing**: Test POST endpoints with JSON payloads
- **WebSocket Testing**: Test WebSocket connections, echo, and message handling
- **Stress Testing**: Measure RPS with configurable concurrency and duration
- **Mixed Workload**: Combined HTTP and WebSocket stress tests

## Building

```bash
cd super-tests
cargo build --release
```

## Usage

### Start the Test Server

First, start the sample PyKrozen server:

```bash
python server.py
```

Or with custom ports:

```bash
HTTP_PORT=8080 WS_PORT=9000 python server.py
```

### Run Tests

#### Health Check

Quick check to verify server is running:

```bash
cargo run -- health
```

#### Run All Tests

Run the complete test suite:

```bash
cargo run -- all
```

With custom parameters:

```bash
cargo run -- all --concurrency 200 --duration 30 --endpoints /,/_internal/health,/data
```

#### HTTP GET Tests

```bash
cargo run -- get
cargo run -- get --paths /,/_internal/health,/data,/echo
```

#### HTTP POST Tests

```bash
cargo run -- post
cargo run -- post --paths /,/echo,/data
```

#### WebSocket Tests

```bash
cargo run -- websocket
cargo run -- websocket --connections 20 --messages 200
```

#### Stress Tests

```bash
# HTTP GET stress test
cargo run -- stress --test-type http-get --concurrency 100 --duration 10

# HTTP POST stress test
cargo run -- stress --test-type http-post --concurrency 100 --duration 10

# WebSocket stress test
cargo run -- stress --test-type websocket --concurrency 50 --duration 10

# Mixed workload
cargo run -- stress --test-type mixed --concurrency 100 --duration 10
```

### Custom Server Configuration

Connect to a different server:

```bash
cargo run -- --host 192.168.1.100 --http-port 8080 --ws-port 9000 all
```

## Test Output

The tool provides detailed output including:

- Success/failure status for each test
- Response times and latency statistics
- Requests per second (RPS)
- Throughput in MB/s
- Error counts and percentages

### Example Output

```
╔═══════════════════════════════════════════════════════════════╗
║                  PyKrozen Test Suite v0.1.0                   ║
║          HTTP & WebSocket Endpoint Testing Tool               ║
╚═══════════════════════════════════════════════════════════════╝

→ Target: HTTP: 127.0.0.1:8000 | WebSocket: 127.0.0.1:8765

═══ Health Check ═══
  Checking HTTP Server... OK (200 OK)
  Checking WebSocket Server... OK

═══ Stress Tests ═══

  HTTP GET Stress Test
    Running 100 concurrent workers for 10s

  ─── Results ───
    Duration:          10.02s
    Total Requests:    125847
    Successful:        125847 (100.0%)
    Failed:                 0 (0%)

    RPS:             12558.23

  ─── Latency ───
    Min:             0.15ms
    Avg:             7.92ms
    Max:            45.23ms

  ─── Throughput ───
    Bytes:          15.23 MB
    Rate:            1.52 MB/s
```

## Test Types

### HTTP Tests

1. **Basic GET**: Tests response status, body, and latency
2. **Query Parameters**: Tests URL query string handling
3. **JSON POST**: Tests JSON payload handling
4. **Large Payloads**: Tests with ~10KB request bodies
5. **Error Handling**: Tests 404 and other error responses

### WebSocket Tests

1. **Basic Connection**: Tests handshake and connection establishment
2. **Echo Test**: Verifies echo functionality
3. **JSON Messages**: Tests various JSON message structures
4. **Multiple Messages**: Tests sequential message handling
5. **Concurrent Connections**: Tests multiple simultaneous connections
6. **Large Payloads**: Tests with ~100KB messages
7. **Rapid Fire**: Tests high-frequency message sending
8. **Connection Stability**: Tests connect/disconnect cycles

### Stress Tests

1. **HTTP GET**: Continuous GET requests
2. **HTTP POST**: Continuous POST requests with JSON
3. **WebSocket**: Persistent connections with message exchanges
4. **Mixed**: Rotating through GET, POST, and WebSocket operations

## Requirements

- Rust 1.70+
- Python 3.12+ (for test server)
- PyKrozen library

## License

BSD 3-Clause (same as PyKrozen)
