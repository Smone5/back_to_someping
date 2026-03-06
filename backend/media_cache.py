"""In-memory media cache for local dev and fallback asset delivery."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Dict

_MAX_ITEMS = 60
_TTL_SECONDS = 15 * 60


@dataclass
class _CachedMedia:
    data: bytes
    mime_type: str
    created_at: float


_CACHE: Dict[str, _CachedMedia] = {}


def _prune() -> None:
    now = time.time()
    expired = [key for key, item in _CACHE.items() if now - item.created_at > _TTL_SECONDS]
    for key in expired:
        _CACHE.pop(key, None)

    if len(_CACHE) <= _MAX_ITEMS:
        return

    # Drop oldest first when over capacity.
    items = sorted(_CACHE.items(), key=lambda kv: kv[1].created_at)
    for key, _ in items[: max(0, len(_CACHE) - _MAX_ITEMS)]:
        _CACHE.pop(key, None)


def store_media(data: bytes, mime_type: str) -> str:
    """Stores media bytes in-memory and returns an opaque id."""
    _prune()
    media_id = uuid.uuid4().hex
    _CACHE[media_id] = _CachedMedia(data=data, mime_type=mime_type, created_at=time.time())
    return media_id


def get_media(media_id: str) -> _CachedMedia | None:
    """Fetches media bytes if present and not expired."""
    _prune()
    return _CACHE.get(media_id)
