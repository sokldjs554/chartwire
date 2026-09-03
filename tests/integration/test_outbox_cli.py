"""``chartwire outbox stats | dlq list | dlq replay --id | bench`` through typer's runner against the
test database, and the scenario H bench at 2 K events (the 100 K run is Phase 2, serial window).
"""

from __future__ import annotations

import asyncio
import json

import pytest
from typer.testing import CliRunner

from chartwire.core.config import Settings
from chartwire.db.repo import outbox as outbox_repo
from chartwire.db.tenant import TenantCtx, tenant_tx
from chartwire.outbox import bench
from chartwire.outbox.cli import app
from tests.integration.test_outbox_poller import emit

pytestmark = pytest.mark.integration

runner = CliRunner()


@pytest.fixture
def cli_env(monkeypatch, migrated_db, tmp_path):
    monkeypatch.setenv("CHARTWIRE_DATABASE_URL", migrated_db.app)
    monkeypatch.setenv("CHARTWIRE_DATABASE_OWNER_URL", migrated_db.owner)
    monkeypatch.setenv("CHARTWIRE_REDIS_URL", migrated_db.redis)
    monkeypatch.setenv("CHARTWIRE_OBJECTSTORE", f"localfs:{tmp_path / 'objects'}")
    return migrated_db


async def run(*args: str) -> tuple[int, str]:
    """The commands call ``asyncio.run``; invoke them off the test's loop, as a shell would."""
    result = await asyncio.to_thread(runner.invoke, app, list(args), catch_exceptions=False)
    return result.exit_code, result.stdout


async def test_stats_dlq_list_and_replay(cli_env, app_engine, tenant_a, redis):
    dead_id = await emit(app_engine, tenant_a.id, "test.cli")
    live_id = await emit(app_engine, tenant_a.id, "test.cli")
    async with tenant_tx(app_engine, TenantCtx.service(tenant_a.id)) as session:
        await outbox_repo.mark_dead(session, dead_id, error="ValueError: boom", attempts=8)

    code, out = await run("stats", "--rebuild-sla")
    assert code == 0
    stats = json.loads(out)
    assert stats["total"]["dead"] == 1 and stats["total"]["pending"] == 1
    assert stats[str(tenant_a.id)]["dead"] == 1 and stats["alerts_sla_rebuilt"] == 0

    code, out = await run("dlq", "list", "--tenant", str(tenant_a.id))
    assert code == 0
    (letter,) = json.loads(out)
    assert letter["outbox_event_id"] == dead_id and letter["attempts"] == 8
    assert letter["last_error"] == "ValueError: boom" and letter["replayed_at"] is None
    assert "payload" not in letter

    code, out = await run("dlq", "replay", "--id", str(dead_id))
    assert code == 0 and f"replayed outbox_event {dead_id}" in out
    code, _ = await run("dlq", "replay", "--id", str(dead_id))
    assert code == 1, "already pending: nothing to replay"
    code, out = await run("dlq", "list")
    assert json.loads(out)[0]["replayed_at"] is not None
    assert json.loads((await run("stats"))[1])["total"]["pending"] == 2
    del live_id


async def test_bench_2k_events_two_workers_writes_report(cli_env, clean_db, tmp_path):
    out = tmp_path / "H.json"
    settings = Settings()
    report = await bench.run_bench(settings, events=2000, workers=2, tenants=5, seed=42, kill_at=0.2, out=out)
    assert out.exists() and json.loads(out.read_text()) == report
    for key in ("seed", "git_sha", "generated_at", "cpu", "ram_gb", "python", "pg_version"):
        assert key in report, "§11.1 report header"
    assert report["events"] == 2000 and report["workers"] == 2 and report["tenants"] == 5
    assert report["events_per_s"] > 0 and report["dlq_count"] == 0
    assert report["worker_killed"] is True and report["reclaimed"] >= 0
    assert report["claim_ms_p50"] is not None and report["claim_ms_p95"] >= report["claim_ms_p50"]
    assert report["cleaned_rows"] == 2000, "bench rows are pruned after the run"
    # tenants are reused across runs (idempotent); events fully processed leaves nothing open
    tenants = await bench.ensure_tenants(settings.database_owner_url, 5)
    assert len(tenants) == 5
    assert await bench.pending_total(clean_db, tenants) == (0, 0)


async def test_bench_cli_rejects_bad_sizes(cli_env):
    with pytest.raises(ValueError):
        await run("bench", "--events", "0", "--tenants", "1")
