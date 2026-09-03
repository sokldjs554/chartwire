"""Filesystem object store for development, tests and the single-process Render deployment."""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path


class LocalFs:
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if path != self.root and self.root not in path.parents:
            raise ValueError(f"key escapes the store root: {key!r}")
        return path

    async def put(self, key: str, data: bytes) -> None:
        await asyncio.to_thread(self._put_sync, self._path(key), data)

    @staticmethod
    def _put_sync(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)  # atomic: a reader never sees a half-written chunk

    async def get(self, key: str) -> bytes:
        return await asyncio.to_thread(self._path(key).read_bytes)

    async def delete_prefix(self, prefix: str) -> int:
        return await asyncio.to_thread(self._delete_prefix_sync, prefix)

    def _delete_prefix_sync(self, prefix: str) -> int:
        keys = self._list_sync(prefix)
        for key in keys:
            self._path(key).unlink(missing_ok=True)
        directory = self._path(prefix)
        if prefix.endswith("/") and directory.is_dir():
            shutil.rmtree(directory, ignore_errors=True)
        return len(keys)

    async def list(self, prefix: str) -> list[str]:
        return await asyncio.to_thread(self._list_sync, prefix)

    def _list_sync(self, prefix: str) -> list[str]:
        base = self._path(prefix)
        start = base if base.is_dir() else base.parent
        if not start.is_dir():
            return []
        keys = [
            p.relative_to(self.root).as_posix()
            for p in start.rglob("*")
            if p.is_file() and p.suffix != ".tmp"
        ]
        return sorted(k for k in keys if k.startswith(prefix))
