from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable
from uuid import UUID


@runtime_checkable
class ObjectStore(Protocol):
    async def put(self, key: str, data: bytes) -> None: ...

    async def get(self, key: str) -> bytes: ...

    async def delete_prefix(self, prefix: str) -> int:
        """Delete every object under ``prefix``; returns the count (purge step 3)."""
        ...

    async def list(self, prefix: str) -> list[str]: ...


def chunk_key(tenant_id: UUID, session_id: UUID, seq: int) -> str:
    return f"{tenant_id}/{session_id}/{seq:08d}.bin"


def session_prefix(tenant_id: UUID, session_id: UUID) -> str:
    return f"{tenant_id}/{session_id}/"


def from_spec(spec: str) -> ObjectStore:
    """``localfs:<dir>`` → :class:`LocalFs`; ``s3://<bucket>`` → :class:`S3` (boto3 required)."""
    if spec.startswith("localfs:"):
        from chartwire.objectstore.localfs import LocalFs

        return LocalFs(Path(spec.removeprefix("localfs:")))
    if spec.startswith("s3://"):
        from chartwire.objectstore.s3 import S3

        return S3(spec.removeprefix("s3://").strip("/"))
    raise ValueError(f"unknown objectstore spec {spec!r} (expected localfs:<dir> or s3://<bucket>)")
