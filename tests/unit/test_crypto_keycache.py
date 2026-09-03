"""KeyCache: hit/miss, LRU eviction at 1000, TTL with an injected clock, invalidation, destroyed DEKs."""

from __future__ import annotations

from uuid import uuid4

import pytest

from chartwire.crypto.envelope import Envelope
from chartwire.crypto.errors import DekDestroyedError
from chartwire.crypto.kek import LocalKek
from chartwire.crypto.keycache import DEFAULT_MAXSIZE, DEFAULT_TTL_S, KeyCache

MASTER = bytes(range(32))
REF = "tenant-a"


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def monotonic(self) -> float:
        return self.t


class CountingKek(LocalKek):
    def __init__(self) -> None:
        super().__init__(MASTER)
        self.unwraps = 0

    def unwrap(self, wrapped: bytes, kek_ref: str) -> bytes:
        self.unwraps += 1
        return super().unwrap(wrapped, kek_ref)


@pytest.fixture
def kek() -> CountingKek:
    return CountingKek()


def test_defaults_match_spec() -> None:
    assert DEFAULT_MAXSIZE == 1000 and DEFAULT_TTL_S == 600.0


def test_miss_then_hit(kek: CountingKek) -> None:
    cache = KeyCache(kek, clock=FakeClock())
    sid, dek = uuid4(), Envelope.new_dek()
    wrapped = kek.wrap(dek, REF)
    assert cache.get(sid, REF, wrapped) == dek
    assert cache.get(sid, REF, wrapped) == dek
    assert kek.unwraps == 1
    assert cache.stats.hits == 1 and cache.stats.misses == 1 and cache.stats.size == 1
    assert sid in cache and str(sid) in cache


def test_ttl_expiry_with_injected_clock(kek: CountingKek) -> None:
    clock = FakeClock()
    cache = KeyCache(kek, ttl_s=600, clock=clock)
    sid = uuid4()
    wrapped = kek.wrap(Envelope.new_dek(), REF)
    cache.get(sid, REF, wrapped)
    clock.t += 599
    cache.get(sid, REF, wrapped)
    assert kek.unwraps == 1 and sid in cache
    clock.t += 1
    assert sid not in cache
    cache.get(sid, REF, wrapped)
    assert kek.unwraps == 2


def test_lru_evicts_least_recently_used(kek: CountingKek) -> None:
    cache = KeyCache(kek, maxsize=2, clock=FakeClock())
    ids = [uuid4() for _ in range(3)]
    wrapped = {i: kek.wrap(Envelope.new_dek(), REF) for i in ids}
    cache.get(ids[0], REF, wrapped[ids[0]])
    cache.get(ids[1], REF, wrapped[ids[1]])
    cache.get(ids[0], REF, wrapped[ids[0]])  # refresh ids[0]
    cache.get(ids[2], REF, wrapped[ids[2]])  # evicts ids[1]
    assert ids[0] in cache and ids[2] in cache and ids[1] not in cache
    assert len(cache) == 2


def test_default_size_is_bounded_at_1000(kek: CountingKek) -> None:
    cache = KeyCache(kek, clock=FakeClock())
    for _ in range(1001):
        cache.get(uuid4(), REF, kek.wrap(Envelope.new_dek(), REF))
    assert len(cache) == 1000


def test_rewrapped_dek_is_not_served_stale(kek: CountingKek) -> None:
    cache = KeyCache(kek, clock=FakeClock())
    sid = uuid4()
    dek1, dek2 = Envelope.new_dek(), Envelope.new_dek()
    assert cache.get(sid, REF, kek.wrap(dek1, REF)) == dek1
    assert cache.get(sid, REF, kek.wrap(dek2, REF)) == dek2
    assert kek.unwraps == 2


def test_invalidate_and_clear(kek: CountingKek) -> None:
    cache = KeyCache(kek, clock=FakeClock())
    sid = uuid4()
    wrapped = kek.wrap(Envelope.new_dek(), REF)
    cache.get(sid, REF, wrapped)
    assert cache.invalidate(sid) is True
    assert cache.invalidate(sid) is False
    cache.get(sid, REF, wrapped)
    assert kek.unwraps == 2
    cache.clear()
    assert len(cache) == 0


def test_destroyed_dek_raises_and_evicts(kek: CountingKek) -> None:
    cache = KeyCache(kek, clock=FakeClock())
    sid = uuid4()
    cache.get(sid, REF, kek.wrap(Envelope.new_dek(), REF))
    with pytest.raises(DekDestroyedError) as info:
        cache.get(sid, REF, None)
    assert info.value.scope_id == str(sid)
    assert sid not in cache


def test_constructor_validation(kek: CountingKek) -> None:
    with pytest.raises(ValueError, match="maxsize"):
        KeyCache(kek, maxsize=0)
    with pytest.raises(ValueError, match="ttl"):
        KeyCache(kek, ttl_s=0)


def test_system_clock_default_works(kek: CountingKek) -> None:
    cache = KeyCache(kek)
    sid = uuid4()
    cache.get(sid, REF, kek.wrap(Envelope.new_dek(), REF))
    assert sid in cache
