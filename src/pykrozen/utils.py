"""Shared utility functions."""

from __future__ import annotations

import json
import threading
from typing import Any

from pykrozen.constants import (
    BUFFER_POOL_SIZE,
    BUFFER_SIZE,
    JSON_CACHE_MAX_DICT_KEYS,
    JSON_CACHE_MAX_ITEM_SIZE,
    JSON_CACHE_MAX_SIZE,
    JSON_SEPARATORS,
)

# Pre-allocated byte arrays for common operations (buffer pooling pattern)
_buffer_pool: list[bytearray] = [
    bytearray(BUFFER_SIZE) for _ in range(BUFFER_POOL_SIZE)
]
_buffer_pool_lock = threading.Lock()
_buffer_pool_index = 0

# Pre-encoded common responses to avoid repeated serialization
_CACHED_RESPONSES: dict[str, bytes] = {}


def _json_dumps(obj: Any) -> str:
    """Serialize object to JSON string."""
    return json.dumps(obj, separators=JSON_SEPARATORS)


def _json_loads(data: bytes | str) -> Any:
    """Parse JSON string to object."""
    return json.loads(data)


def _json_dumps_cached(obj: Any) -> bytes:
    """JSON encode with caching for common small objects."""
    if isinstance(obj, dict) and len(obj) <= JSON_CACHE_MAX_DICT_KEYS:
        try:
            key = str(sorted(obj.items()))
            cached = _CACHED_RESPONSES.get(key)
            if cached is not None:
                return cached
            result = json.dumps(obj, separators=JSON_SEPARATORS).encode()
            if len(result) < JSON_CACHE_MAX_ITEM_SIZE:
                if len(_CACHED_RESPONSES) >= JSON_CACHE_MAX_SIZE:
                    keys = list(_CACHED_RESPONSES.keys())
                    for k in keys[: len(keys) // 2]:
                        del _CACHED_RESPONSES[k]
                _CACHED_RESPONSES[key] = result
            return result
        except (TypeError, ValueError):
            pass
    return json.dumps(obj, separators=JSON_SEPARATORS).encode()


def _get_pooled_buffer(size: int = BUFFER_SIZE) -> bytearray:
    """Get a buffer from the pool or create new one."""
    global _buffer_pool_index
    if size <= BUFFER_SIZE:
        with _buffer_pool_lock:
            buf = _buffer_pool[_buffer_pool_index]
            _buffer_pool_index = (_buffer_pool_index + 1) % BUFFER_POOL_SIZE
            return buf
    return bytearray(size)


def _xor_unmask_fast(payload: bytearray, mask: bytes) -> bytes:
    """Optimized XOR unmask using 64-bit operations where possible."""
    length = len(payload)
    if length == 0:
        return b""

    mask8 = mask * 2
    mask_int = int.from_bytes(mask8, "little")

    i = 0
    chunks = length // 8
    for _ in range(chunks):
        chunk_int = int.from_bytes(payload[i : i + 8], "little")
        result_int = chunk_int ^ mask_int
        payload[i : i + 8] = result_int.to_bytes(8, "little")
        i += 8

    for j in range(i, length):
        payload[j] ^= mask[j & 3]

    return bytes(payload)
