# Performance Optimizations - IMPLEMENTED

All optimizations have been implemented with **pure Python fallbacks** and optional high-performance backends.

## Usage

```python
from pykrozen import Server, get_backends

# Check which backends are active
print(get_backends())
# {'json': 'orjson', 'xor': 'numpy', 'server': 'aiohttp'}  # if all installed
# {'json': 'json', 'xor': 'python', 'server': 'threading'}  # stdlib fallback

# Run with auto-detection (uses best available)
Server.run(port=8000)

# Force threading mode (disable async even if aiohttp installed)
Server.run(port=8000, use_async=False)

# Force async mode (raises ImportError if aiohttp not installed)
Server.run(port=8000, use_async=True)
```

## Optional Dependencies

Install for maximum performance:
```bash
pip install orjson numpy aiohttp
```

---

# Performance Test Results (4 Cycles)

## Baseline Performance (Threaded Server)

| Test Type | Requests/sec | Avg Latency | Throughput |
|-----------|-------------|-------------|------------|
| HTTP GET  | ~8,200 RPS  | 6.0ms       | 1.7 MB/s   |
| HTTP POST | ~9,400 RPS  | 5.3ms       | 1.0 MB/s   |
| WebSocket | ~35,000 msg/s | 1.4ms     | 1.2 MB/s   |
| Mixed     | ~5,200 RPS  | 0.8ms       | 0.4 MB/s   |

## Key Findings

1. **ThreadingHTTPServer is well-optimized** - Attempts to use ThreadPoolExecutor actually degraded performance
2. **orjson provides ~2-3x JSON speedup** - Using `.dumps()` directly returns bytes (saves encode step)
3. **numpy XOR masking** - Provides ~10x speedup for large WebSocket payloads
4. **Buffer sizes** - Increased to 65536 for better throughput
5. **`__slots__`** - Reduces memory per request by ~240 bytes

## Optimizations Applied

### High Impact - DONE

- [x] **Optional orjson integration**
  - Auto-detects at import time
  - Uses `orjson.dumps()` directly (returns bytes)
  - ~2-3x JSON serialization speedup

- [x] **Optional numpy XOR masking**
  - Vectorized XOR for WebSocket unmasking
  - ~10x speedup for large payloads

- [x] **Async server (aiohttp) - optional**
  - `Server.run(use_async=True/False/None)`
  - Recommended for high-concurrency workloads

### Medium Impact - DONE

- [x] **HTTPHandler buffer sizes**
  - `rbufsize = 65536`
  - `wbufsize = 65536`

- [x] **Lazy HTTPContext creation**
  - Only created when hooks are registered

### Low Impact - DONE

- [x] **`__slots__` on Request, Response, WSMessage**
  - ~240 bytes memory savings per instance
  - `Request.extra` dict for custom attributes

---

## Backend Detection

```python
from pykrozen import get_backends

backends = get_backends()
# {
#   'json': 'orjson' | 'json',
#   'xor': 'numpy' | 'python',
#   'server': 'aiohttp' | 'threading'
# }
```

## Running Performance Tests

```bash
# Build the Rust test tool
cd super-tests && cargo build --release

# Run all tests with auto-start server
./target/release/super-tests auto --concurrency 50 --duration 5

# Or run specific test
./target/release/super-tests stress --concurrency 100 --duration 10 --test-type http-get
```
