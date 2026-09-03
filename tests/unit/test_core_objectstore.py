from uuid import UUID

import pytest

from chartwire.objectstore import LocalFs, ObjectStore, chunk_key, from_spec, session_prefix

T = UUID(int=1)
S = UUID(int=2)


def test_key_layout():
    assert chunk_key(T, S, 17) == f"{T}/{S}/00000017.bin"
    assert session_prefix(T, S) == f"{T}/{S}/"


async def test_localfs_round_trip_list_and_delete_prefix(tmp_path):
    store: ObjectStore = from_spec(f"localfs:{tmp_path / 'objects'}")
    assert isinstance(store, LocalFs)
    for seq in (2, 1):
        await store.put(chunk_key(T, S, seq), bytes([seq]))
    await store.put(chunk_key(T, UUID(int=3), 1), b"other")
    assert await store.get(chunk_key(T, S, 2)) == b"\x02"
    assert await store.list(session_prefix(T, S)) == [chunk_key(T, S, 1), chunk_key(T, S, 2)]
    assert await store.delete_prefix(session_prefix(T, S)) == 2
    assert await store.list(session_prefix(T, S)) == []
    assert await store.get(chunk_key(T, UUID(int=3), 1)) == b"other"
    assert await store.delete_prefix(session_prefix(T, S)) == 0


async def test_localfs_rejects_path_escape(tmp_path):
    store = LocalFs(tmp_path)
    with pytest.raises(ValueError):
        await store.put("../escape.bin", b"x")


def test_unknown_spec():
    with pytest.raises(ValueError):
        from_spec("gcs://bucket")
