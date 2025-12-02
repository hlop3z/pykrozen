"""File serving with caching."""

from __future__ import annotations

import os
from pathlib import Path

from pykrozen.constants import (
    CONTENT_TYPE_OCTET,
    ERROR_FILE_NOT_FOUND,
    HEADER_CONTENT_TYPE,
    MIME_TYPES,
)
from pykrozen.http import Response, error

_cache: dict[str, tuple[bytes, str]] = {}


def clear_cache(filepath: str | Path | None = None) -> None:
    """Clear file cache (all or specific file)."""
    if filepath is None:
        _cache.clear()
    else:
        _cache.pop(str(filepath), None)


def _get_mime_type(filepath: str) -> str:
    """Get MIME type from extension."""
    return MIME_TYPES.get(os.path.splitext(filepath)[1].lower(), CONTENT_TYPE_OCTET)


def file(
    filepath: Path,
    content_type: str | None = None,
    status: int = 200,
    reload: bool = False,
) -> Response:
    """Serve file with caching. Returns 404 if not found."""
    key = str(filepath)

    if not reload and key in _cache:
        content, cached_type = _cache[key]
        return {
            "body": content,
            "headers": {HEADER_CONTENT_TYPE: content_type or cached_type},
            "status": status,
        }

    try:
        with open(key, "rb") as f:
            content = f.read()
    except FileNotFoundError:
        return error(ERROR_FILE_NOT_FOUND, 404)

    resolved_type = content_type or _get_mime_type(key)
    if not reload:
        _cache[key] = (content, resolved_type)

    return {
        "body": content,
        "headers": {HEADER_CONTENT_TYPE: resolved_type},
        "status": status,
    }
