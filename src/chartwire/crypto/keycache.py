"""Per-process LRU cache of unwrapped DEKs (§3.1, §8.2).

Entries are keyed by scope id (session/patient/tenant) and remembered together
with the fingerprint of the wrapped DEK they were unwrapped from, so a rewrapped
key never serves a stale plaintext key. ``invalidate`` is what the
``keys:invalidate`` pub/sub subscriber calls after a purge; passing
``wrapped=None`` (the ``dek_wrapped IS NULL`` tombstone) evicts and raises
:class:`DekDestroyedError` so a destroyed DEK can never be served from cache.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Final, Protocol
from uuid import UUID

from chartwire.crypto.envelope import dek_fingerprint
from chartwire.crypto.errors import DekDestroyedError
from chartwire.crypto.kek import KekProvider

DEFAULT_MAXSIZE: Final = 1000
DEFAULT_TTL_S: Final = 600.0


class MonotonicClock(Protocol):
    def monotonic(self) -> float: ...


@dataclass(slots=True)
class _Entry:
    fingerprint: bytes
    dek: bytes
    expires_at: float


@dataclass(frozen=True, slots=True)
class CacheStats:
    hits: int
    misses: int
    size: int


class KeyCache:
    def __init__(
        self,
        kek: KekProvider,
        *,
        maxsize: int = DEFAULT_MAXSIZE,
        ttl_s: float = DEFAULT_TTL_S,
        clock: MonotonicClock | None = None,
    ) -> None:
        if maxsize < 1:
            raise ValueError("maxsize must be >= 1")
        if ttl_s <= 0:
            raise ValueError("ttl_s must be > 0")
        self._kek = kek
        self._maxsize = maxsize
        self._ttl_s = ttl_s
        self._clock = clock
        self._entries: OrderedDict[str, _Entry] = OrderedDict()
        self._hits = 0
        self._misses = 0

    def _now(self) -> float:
        return self._clock.monotonic() if self._clock is not None else time.monotonic()

    def get(self, scope_id: UUID | str, kek_ref: str, wrapped: bytes | None) -> bytes:
        """Return the plaintext DEK for ``scope_id``, unwrapping (and caching) on a miss."""
        key = str(scope_id)
        if wrapped is None:
            self._entries.pop(key, None)
            raise DekDestroyedError(key)
        fingerprint = dek_fingerprint(wrapped)
        now = self._now()
        entry = self._entries.get(key)
        if entry is not None and entry.fingerprint == fingerprint and entry.expires_at > now:
            self._entries.move_to_end(key)
            self._hits += 1
            return entry.dek
        self._misses += 1
        dek = self._kek.unwrap(wrapped, kek_ref)
        self._entries.pop(key, None)
        self._entries[key] = _Entry(fingerprint, dek, now + self._ttl_s)
        while len(self._entries) > self._maxsize:
            self._entries.popitem(last=False)
        return dek

    def invalidate(self, scope_id: UUID | str) -> bool:
        """Drop the entry for ``scope_id`` (payload of ``keys:invalidate``). True if something was cached."""
        return self._entries.pop(str(scope_id), None) is not None

    def clear(self) -> None:
        self._entries.clear()

    def __contains__(self, scope_id: object) -> bool:
        entry = self._entries.get(str(scope_id))
        return entry is not None and entry.expires_at > self._now()

    def __len__(self) -> int:
        return len(self._entries)

    @property
    def stats(self) -> CacheStats:
        return CacheStats(hits=self._hits, misses=self._misses, size=len(self._entries))
