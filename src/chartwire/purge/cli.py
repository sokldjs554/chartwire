"""``chartwire purge run --session|--patient · receipt --job · verify --job`` (§13.1).

Mounted lazily by ``chartwire.cli`` (``LAZY_SUBAPPS["purge"]``). ``run`` executes the pipeline
in-process (no worker needed — useful for a demo box and for the eval harness); ``receipt`` prints
the 파기 영수증 JSON; ``verify`` runs step 7 and exits 1 when a check fails. Output carries ids,
counts, hashes and check names only.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from uuid import UUID

import typer

from chartwire.core.config import Settings, get_settings
from chartwire.db.models import PurgeJob
from chartwire.db.repo import purge as purge_repo
from chartwire.db.tenant import TenantCtx, tenant_tx
from chartwire.outbox.context import HandlerContext
from chartwire.outbox.runtime import active_tenant_ids, build_context, close_context
from chartwire.purge import pipeline, receipt, verify

app = typer.Typer(help="파기(crypto-shred + hard delete) 실행 · 영수증 · 검증", no_args_is_help=True)


def _echo_json(data: Any) -> None:
    typer.echo(json.dumps(data, ensure_ascii=False, indent=2, default=str))


async def _find_job_tenant(ctx: HandlerContext, job_id: UUID) -> UUID | None:
    """``purge_jobs`` is RLS-protected; without ``--tenant`` iterate the active tenants (ADR-0001)."""
    for tenant_id in await active_tenant_ids(ctx.engine):
        async with tenant_tx(ctx.engine, TenantCtx.service(tenant_id)) as session:
            if await purge_repo.get_job(session, job_id) is not None:
                return tenant_id
    return None


async def _tenant_for(ctx: HandlerContext, job_id: UUID, tenant: UUID | None) -> UUID:
    tenant_id = tenant or await _find_job_tenant(ctx, job_id)
    if tenant_id is None:
        raise typer.BadParameter(f"파기 작업 {job_id} 을(를) 찾을 수 없습니다 (--tenant 를 지정하세요)")
    return tenant_id


async def run_purge(
    settings: Settings, *, tenant_id: UUID, subject_type: str, subject_id: UUID, do_verify: bool
) -> dict[str, Any]:
    ctx = build_context(settings, pool_size=2)
    try:
        async with ctx.tenant_tx(tenant_id) as session:
            job = await pipeline.create_job(
                session,
                tenant_id=tenant_id,
                subject_type=subject_type,
                subject_id=subject_id,
                reason="admin",
                requested_by=None,
            )
        job = await pipeline.run(ctx, job.id, tenant_id=tenant_id)
        if do_verify:
            try:
                job = await verify.run(ctx, job.id, tenant_id=tenant_id)
            except verify.PurgeVerifyFailed:
                async with ctx.tenant_tx(tenant_id) as session:
                    refreshed = await purge_repo.get_job(session, job.id)
                assert refreshed is not None
                job = refreshed
        return receipt.build(job)
    finally:
        await close_context(ctx)


async def load_receipt(settings: Settings, *, job_id: UUID, tenant: UUID | None) -> dict[str, Any]:
    ctx = build_context(settings, pool_size=2)
    try:
        tenant_id = await _tenant_for(ctx, job_id, tenant)
        async with ctx.tenant_tx(tenant_id) as session:
            job = await purge_repo.get_job(session, job_id)
        assert job is not None
        return receipt.build(job)
    finally:
        await close_context(ctx)


async def run_verify(settings: Settings, *, job_id: UUID, tenant: UUID | None) -> dict[str, Any]:
    ctx = build_context(settings, pool_size=2)
    try:
        tenant_id = await _tenant_for(ctx, job_id, tenant)
        try:
            job: PurgeJob | None = await verify.run(ctx, job_id, tenant_id=tenant_id)
        except verify.PurgeVerifyFailed:
            async with ctx.tenant_tx(tenant_id) as session:
                job = await purge_repo.get_job(session, job_id)
        assert job is not None
        return receipt.build(job)
    finally:
        await close_context(ctx)


@app.command("run")
def run_cmd(
    tenant: UUID = typer.Option(..., "--tenant", help="테넌트 id"),
    session: UUID | None = typer.Option(None, "--session", help="세션 id (세션 단위 파기)"),
    patient: UUID | None = typer.Option(None, "--patient", help="환자 id (환자 단위: 모든 세션 + 식별자)"),
    no_verify: bool = typer.Option(False, "--no-verify", help="7단계 검증을 생략"),
) -> None:
    """파기 작업을 만들고 즉시 실행한 뒤(기본: 검증까지) 영수증을 출력합니다."""
    if (session is None) == (patient is None):
        raise typer.BadParameter("--session 또는 --patient 중 하나만 지정하세요")
    subject_type, subject_id = ("session", session) if session is not None else ("patient", patient)
    assert subject_id is not None
    out = asyncio.run(
        run_purge(
            get_settings(),
            tenant_id=tenant,
            subject_type=subject_type,
            subject_id=subject_id,
            do_verify=not no_verify,
        )
    )
    _echo_json(out)
    if out["state"] == "failed":
        raise typer.Exit(code=1)


@app.command("receipt")
def receipt_cmd(
    job: UUID = typer.Option(..., "--job", help="purge_jobs.id"),
    tenant: UUID | None = typer.Option(None, "--tenant", help="테넌트 id (생략 시 활성 테넌트 순회)"),
) -> None:
    """파기 영수증(steps, counts, dek_fingerprints, receipt_hash, verify_result)을 출력합니다."""
    _echo_json(asyncio.run(load_receipt(get_settings(), job_id=job, tenant=tenant)))


@app.command("verify")
def verify_cmd(
    job: UUID = typer.Option(..., "--job", help="purge_jobs.id"),
    tenant: UUID | None = typer.Option(None, "--tenant", help="테넌트 id (생략 시 활성 테넌트 순회)"),
) -> None:
    """§8.4 7단계 검증을 실행합니다. 하나라도 실패하면 종료 코드 1."""
    out = asyncio.run(run_verify(get_settings(), job_id=job, tenant=tenant))
    _echo_json(out)
    if out["state"] != "verified":
        raise typer.Exit(code=1)
