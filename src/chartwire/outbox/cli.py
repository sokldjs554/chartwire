"""``chartwire outbox stats [--rebuild-sla] | dlq list | dlq replay --id | bench`` (§13.1).

Mounted lazily by ``chartwire.cli`` (``LAZY_SUBAPPS["outbox"]``). Output never contains payloads or
transcript text: ids, event types, attempts, counts and the (already redacted) ``last_error``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from uuid import UUID

import typer

from chartwire.core.config import Settings, get_settings
from chartwire.db.engine import make_engine
from chartwire.db.repo import risk as risk_repo
from chartwire.db.tenant import TenantCtx, tenant_tx
from chartwire.outbox import dlq
from chartwire.outbox.runtime import active_tenant_ids
from chartwire.redis import keys
from chartwire.redis.client import get_redis

app = typer.Typer(help="아웃박스 상태 · DLQ · 시나리오 H 벤치", no_args_is_help=True)
dlq_app = typer.Typer(help="dead_letters 조회 / 재처리", no_args_is_help=True)
app.add_typer(dlq_app, name="dlq")


def _echo_json(data: Any) -> None:
    typer.echo(json.dumps(data, ensure_ascii=False, indent=2, default=str))


async def rebuild_sla(settings: Settings) -> int:
    """Re-``ZADD`` ``alerts:sla`` from open, un-escalated ``risk_events`` (runbook §3-5, Redis loss)."""
    engine = make_engine(settings.database_url, pool_size=2)
    redis: Any = get_redis(settings.redis_url)
    added = 0
    try:
        for tenant_id in await active_tenant_ids(engine):
            async with tenant_tx(engine, TenantCtx.service(tenant_id)) as session:
                rows = await risk_repo.list_open(session, tenant_id, limit=10_000)
            members: dict[str | bytes, bytes | float | int | str] = {
                keys.alerts_sla_member(tenant_id, row.id): row.sla_deadline_at.timestamp() * 1000.0
                for row in rows
                if row.sla_deadline_at is not None and row.escalation_level == 0
            }
            if members:
                added += int(await redis.zadd(keys.ALERTS_SLA, members))
    finally:
        await engine.dispose()
        await redis.aclose()
    return added


async def _stats(settings: Settings, rebuild: bool) -> dict[str, Any]:
    engine = make_engine(settings.database_url, pool_size=2)
    try:
        out: dict[str, Any] = dict(await dlq.stats_all(engine))
    finally:
        await engine.dispose()
    if rebuild:
        out["alerts_sla_rebuilt"] = await rebuild_sla(settings)
    return out


@app.command("stats")
def stats(
    rebuild_sla_flag: bool = typer.Option(
        False, "--rebuild-sla", help="risk_events 로부터 alerts:sla ZSET 재구축 (Redis 손실 복구)"
    ),
) -> None:
    """테넌트별 pending/in_flight/done/dead 수와 가장 오래된 pending 행의 나이."""
    _echo_json(asyncio.run(_stats(get_settings(), rebuild_sla_flag)))


async def _dlq_list(settings: Settings, tenant: UUID | None, limit: int) -> list[dict[str, Any]]:
    engine = make_engine(settings.database_url, pool_size=2)
    try:
        rows = await dlq.list_dead(engine, tenant_id=tenant, limit=limit)
    finally:
        await engine.dispose()
    return [
        {
            "id": r.id,
            "outbox_event_id": r.outbox_event_id,
            "tenant_id": str(r.tenant_id),
            "event_type": r.event_type,
            "attempts": r.attempts,
            "last_error": r.last_error,
            "died_at": r.died_at.isoformat(),
            "replayed_at": r.replayed_at.isoformat() if r.replayed_at else None,
        }
        for r in rows
    ]


@dlq_app.command("list")
def dlq_list(
    tenant: UUID | None = typer.Option(None, "--tenant", help="테넌트 id (생략: 활성 테넌트 전부)"),
    limit: int = typer.Option(50, "--limit"),
) -> None:
    """dead_letters 를 최신순으로 출력 (payload 는 출력하지 않음)."""
    _echo_json(asyncio.run(_dlq_list(get_settings(), tenant, limit)))


async def _dlq_replay(settings: Settings, event_id: int, tenant: UUID | None) -> bool:
    engine = make_engine(settings.database_url, pool_size=2)
    try:
        return await dlq.replay(engine, event_id, tenant_id=tenant)
    finally:
        await engine.dispose()


@dlq_app.command("replay")
def dlq_replay(
    event_id: int = typer.Option(..., "--id", help="outbox_events.id"),
    tenant: UUID | None = typer.Option(None, "--tenant"),
) -> None:
    """dead → pending (attempts=0, next_attempt_at=now); 핸들러가 멱등이므로 안전."""
    if asyncio.run(_dlq_replay(get_settings(), event_id, tenant)):
        typer.echo(f"replayed outbox_event {event_id}")
        return
    typer.echo(f"no dead outbox_event {event_id}", err=True)
    raise typer.Exit(1)


@app.command("bench")
def bench(
    events: int = typer.Option(100_000, "--events"),
    workers: int = typer.Option(2, "--workers"),
    tenants: int = typer.Option(30, "--tenants"),
    seed: int = typer.Option(42, "--seed"),
    kill_at: float = typer.Option(0.25, "--kill-at", help="이 진행률에서 워커 하나를 중단 (0 = 안 함)"),
    out: Path = typer.Option(Path("docs/loadtest/H.json"), "--out"),
) -> None:
    """시나리오 H: noop 이벤트 N 개 · 워커 K · 테넌트 T → events/s, DLQ, reclaim → H.json."""
    from chartwire.outbox.bench import run_bench

    report = asyncio.run(
        run_bench(
            get_settings(),
            events=events,
            workers=workers,
            tenants=tenants,
            seed=seed,
            kill_at=kill_at or None,
            out=out,
        )
    )
    _echo_json(
        {
            k: report[k]
            for k in ("events", "workers", "tenants", "elapsed_s", "events_per_s", "dlq_count", "reclaimed")
        }
    )
