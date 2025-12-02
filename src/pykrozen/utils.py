"""Shared utility functions."""

from __future__ import annotations

import json
from typing import Any

from pykrozen.constants import (
    JSON_CACHE_MAX_DICT_KEYS,
    JSON_CACHE_MAX_ITEM_SIZE,
    JSON_CACHE_MAX_SIZE,
    JSON_SEPARATORS,
)

# Response cache for common small objects
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
            if key in _CACHED_RESPONSES:
                return _CACHED_RESPONSES[key]
            result = json.dumps(obj, separators=JSON_SEPARATORS).encode()
            if len(result) < JSON_CACHE_MAX_ITEM_SIZE:
                if len(_CACHED_RESPONSES) >= JSON_CACHE_MAX_SIZE:
                    # Evict half the cache
                    keys = list(_CACHED_RESPONSES.keys())
                    for k in keys[: len(keys) // 2]:
                        del _CACHED_RESPONSES[k]
                _CACHED_RESPONSES[key] = result
            return result
        except (TypeError, ValueError):
            pass
    return json.dumps(obj, separators=JSON_SEPARATORS).encode()


def _xor_unmask(payload: bytearray, mask: bytes) -> bytes:
    """XOR unmask WebSocket payload with 64-bit optimization."""
    length = len(payload)
    if length == 0:
        return b""

    # Process 8 bytes at a time using 64-bit operations
    mask8 = mask * 2
    mask_int = int.from_bytes(mask8, "little")

    i = 0
    for _ in range(length // 8):
        chunk = int.from_bytes(payload[i : i + 8], "little")
        payload[i : i + 8] = (chunk ^ mask_int).to_bytes(8, "little")
        i += 8

    # Handle remaining bytes
    for j in range(i, length):
        payload[j] ^= mask[j & 3]

    return bytes(payload)
