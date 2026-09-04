"""``chartwire audit list --tenant <id> [--action] [--limit] [--before]`` (§13.1).

Reads ``audit_events`` under a ``service`` context for one tenant (the RESTRICTIVE
``audit_read_gate`` admits ``auditor``/``admin``/``service``). Rows carry ids, actions and counts
only — the ``detail`` column is PHI-free by construction (``audit.service.assert_no_phi``).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from uuid import UUID

import typer

from chartwire.core.config import Settings, get_settings
from chartwire.db.engine import make_engine
from chartwire.db.repo import audit as audit_repo
from chartwire.db.tenant import TenantCtx, tenant_tx

app = typer.Typer(help="감사 로그 조회", no_args_is_help=True)


async def list_events(
    settings: Settings, *, tenant_id: UUID, action: str | None, limit: int, before: int | None
) -> list[dict[str, Any]]:
    engine = make_engine(settings.database_url, pool_size=2)
    try:
        async with tenant_tx(engine, TenantCtx.service(tenant_id)) as session:
            rows = await audit_repo.list_events(session, tenant_id, before=before, limit=limit, action=action)
        return [
            {
                "id": int(row.id),
                "at": row.at.isoformat(),
                "actor_id": None if row.actor_id is None else str(row.actor_id),
                "actor_role": row.actor_role,
                "action": row.action,
                "resource_type": row.resource_type,
                "resource_id": row.resource_id,
                "request_id": row.request_id,
                "detail": dict(row.detail or {}),
            }
            for row in rows
        ]
    finally:
        await engine.dispose()


@app.command("list")
def list_cmd(
    tenant: UUID = typer.Option(..., "--tenant", help="테넌트 id"),
    action: str | None = typer.Option(None, "--action", help="예: purge.completed"),
    limit: int = typer.Option(100, "--limit", min=1, max=1000),
    before: int | None = typer.Option(None, "--before", help="이 id 보다 작은 행만 (keyset)"),
) -> None:
    """최신순 감사 이벤트를 JSON 배열로 출력합니다."""
    rows = asyncio.run(
        list_events(get_settings(), tenant_id=tenant, action=action, limit=limit, before=before)
    )
    typer.echo(json.dumps(rows, ensure_ascii=False, indent=2))
