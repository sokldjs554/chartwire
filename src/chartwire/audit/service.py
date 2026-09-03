"""Audit recording (spec §8.5). ``detail`` carries ids, counts and hashes — never PHI."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from chartwire.db.repo import audit as audit_repo

PHI_DETAIL_KEYS: frozenset[str] = frozenset({"text", "quote", "name", "phone"})


def assert_no_phi(detail: dict[str, Any], path: str = "detail") -> None:
    """Raise ``ValueError`` if a forbidden key appears anywhere in ``detail`` (nested dicts/lists too)."""
    for key, value in detail.items():
        if key in PHI_DETAIL_KEYS:
            raise ValueError(f"audit {path}.{key}: PHI keys are not allowed in audit details")
        if isinstance(value, dict):
            assert_no_phi(value, f"{path}.{key}")
        elif isinstance(value, list):
            for i, item in enumerate(value):
                if isinstance(item, dict):
                    assert_no_phi(item, f"{path}.{key}[{i}]")


async def record(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    actor_id: UUID | None,
    actor_role: str | None,
    action: str,
    resource_type: str,
    resource_id: str | UUID | None,
    request_id: str | None = None,
    detail: dict[str, Any] | None = None,
) -> int:
    """Append one audit row in the caller's transaction; returns its id."""
    if detail:
        assert_no_phi(detail)
    return await audit_repo.record(
        session,
        tenant_id=tenant_id,
        actor_id=actor_id,
        actor_role=actor_role,
        action=action,
        resource_type=resource_type,
        resource_id=None if resource_id is None else str(resource_id),
        request_id=request_id,
        detail=detail,
    )
