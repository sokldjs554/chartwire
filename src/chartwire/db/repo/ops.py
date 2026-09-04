"""Catalog views for the admin ops endpoints (§6.9 ``/ops/partitions``) — no tenant data."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_PARTITIONS_SQL = text(
    "SELECT c.relname AS name, pg_get_expr(c.relpartbound, c.oid) AS bounds "
    "FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid JOIN pg_class p ON p.oid = i.inhparent "
    "WHERE p.relname = 'transcript_segments' ORDER BY c.relname"
)


async def list_segment_partitions(session: AsyncSession) -> list[tuple[str, str | None]]:
    """``(partition name, partition bound expression)`` for every child of ``transcript_segments``."""
    return [(str(name), bounds) for name, bounds in (await session.execute(_PARTITIONS_SQL)).all()]
