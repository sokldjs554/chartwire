"""S3 object store (SSE-KMS bucket from the CDK stack). boto3 is optional and imported lazily;
the class is exercised only against a real bucket, never offline."""

from __future__ import annotations

import asyncio
from typing import Any


class S3:
    def __init__(self, bucket: str, *, client: Any | None = None) -> None:
        if client is None:
            try:
                import boto3
            except ImportError as exc:  # pragma: no cover - optional dependency
                raise RuntimeError("s3:// objectstore needs the 'aws' extra (boto3)") from exc
            client = boto3.client("s3")
        self.bucket = bucket
        self._client = client

    async def put(self, key: str, data: bytes) -> None:
        await asyncio.to_thread(self._client.put_object, Bucket=self.bucket, Key=key, Body=data)

    async def get(self, key: str) -> bytes:
        response = await asyncio.to_thread(self._client.get_object, Bucket=self.bucket, Key=key)
        return bytes(response["Body"].read())

    async def list(self, prefix: str) -> list[str]:
        return await asyncio.to_thread(self._list_sync, prefix)

    def _list_sync(self, prefix: str) -> list[str]:
        keys: list[str] = []
        paginator = self._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            keys.extend(obj["Key"] for obj in page.get("Contents", []))
        return keys

    async def delete_prefix(self, prefix: str) -> int:
        keys = await self.list(prefix)
        for start in range(0, len(keys), 1000):  # DeleteObjects caps at 1000 keys
            batch = [{"Key": k} for k in keys[start : start + 1000]]
            await asyncio.to_thread(
                self._client.delete_objects, Bucket=self.bucket, Delete={"Objects": batch, "Quiet": True}
            )
        return len(keys)
