"""The stt-worker process (spec §7.4): ``chartwire serve stt-worker`` / ``python -m chartwire.stt.worker``.

* discovers sessions from ``stt:active`` once a second;
* takes ownership with ``SET stt:owner:{sid} <worker_id> NX PX 30000`` and renews the lease every
  10 s (compare-and-expire in Lua, so a lease that expired and moved to another worker is never
  renewed by mistake);
* runs one :class:`~chartwire.stt.consumer.SessionConsumer` task per owned session;
* on SIGTERM stops discovery, lets every consumer finish its current entry, releases the leases and
  exits (0 = within the drain budget). A stalled worker (SIGSTOP) simply stops renewing: 30 s later
  another worker acquires the session and ``XAUTOCLAIM``s the entries it left pending.

Configuration comes from ``Settings`` plus ``CHARTWIRE_STT_*`` environment variables read by
:class:`SttWorkerConfig` (see ``docs/stt-providers.md``).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
import sys
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC
from pathlib import Path
from typing import Any
from uuid import UUID

import uvicorn

from chartwire.core.config import Settings
from chartwire.db.models import Tenant
from chartwire.db.repo import sessions as sessions_repo
from chartwire.db.tenant import TenantCtx, tenant_tx
from chartwire.ops import metrics
from chartwire.ops.drain import DEFAULT_DEADLINE_S, Drainer
from chartwire.ops.health import Health
from chartwire.ops.routes import build_health, chain_signals, standalone_app
from chartwire.outbox.runtime import active_tenant_ids, build_context, close_context
from chartwire.redis import keys
from chartwire.stt.base import SttAdapter
from chartwire.stt.consumer import ConsumerConfig, ConsumerStats, SessionConsumer, SessionFacts

log = logging.getLogger(__name__)

PROVIDERS = ("simulator", "slow", "aws")
DEFAULT_SCRIPTS_DIR = "var/scripts"
DEFAULT_HTTP_PORT = 9002
ENV_PREFIX = "CHARTWIRE_STT_"

_RENEW_LUA = """
local v = redis.call('GET', KEYS[1])
if v == ARGV[1] then return redis.call('PEXPIRE', KEYS[1], ARGV[2]) end
if v then return 0 end
if redis.call('SISMEMBER', KEYS[2], ARGV[3]) == 1 then redis.call('SET', KEYS[1], ARGV[1], 'PX', ARGV[2]) return 1 end
return 0
"""
"""Compare-and-expire. A lease that vanished is re-taken only while the session is still listed in
``stt:active`` (Redis flushed mid-session); one held by another worker, or one released because the
session finished, is never re-taken — a thawed ex-owner must stand down, not resurrect the session."""
_RELEASE_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) end
return 0
"""


def _env_int(env: Mapping[str, str], name: str, default: int) -> int:
    return int(env.get(ENV_PREFIX + name, default))


def _env_bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(ENV_PREFIX + name)
    return default if raw is None else raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True, slots=True)
class SttWorkerConfig:
    provider: str = "simulator"
    scripts_dir: Path = Path(DEFAULT_SCRIPTS_DIR)
    slow_delay_ms: int = 400
    sim_seed: int = 0
    sim_latency: bool = True
    aws_region: str = "ap-northeast-2"
    fetch_audio: bool = False
    lease_ms: int = keys.TTL_STT_OWNER_MS
    autoclaim_idle_ms: int = 60_000
    discovery_s: float = 1.0
    read_count: int = 32
    block_ms: int = 1000
    rebuild_wait_s: float = 0.2
    http_port: int = DEFAULT_HTTP_PORT

    @property
    def renew_every_s(self) -> float:
        return self.lease_ms / 3000.0  # 30 s lease → every 10 s (§5)

    @classmethod
    def from_env(cls, settings: Settings, env: Mapping[str, str] | None = None) -> SttWorkerConfig:
        """``CHARTWIRE_STT_PROVIDER`` (simulator|slow|aws), ``_SCRIPTS_DIR``, ``_SLOW_DELAY_MS``,
        ``_SIM_SEED``, ``_SIM_LATENCY``, ``_AWS_REGION``, ``_FETCH_AUDIO``, ``_LEASE_MS``,
        ``_AUTOCLAIM_IDLE_MS``, ``_WORKER_PORT`` (0 = no ops HTTP server)."""
        env = os.environ if env is None else env
        # ``Settings`` (env prefix ``CHARTWIRE_``) already carries ``stt_provider`` / ``stt_scripts_dir`` /
        # ``stt_slow_delay_ms`` / ``stt_sim_seed`` / ``stt_worker_port`` (WP-C request 3); the explicit
        # ``env`` mapping still wins so tests can inject values without touching the process environment.
        provider = env.get(ENV_PREFIX + "PROVIDER", settings.stt_provider).strip().lower()
        if provider not in PROVIDERS:
            raise ValueError(f"CHARTWIRE_STT_PROVIDER must be one of {PROVIDERS}, got {provider!r}")
        return cls(
            provider=provider,
            scripts_dir=Path(env.get(ENV_PREFIX + "SCRIPTS_DIR", settings.stt_scripts_path)),
            slow_delay_ms=_env_int(env, "SLOW_DELAY_MS", settings.stt_slow_delay_ms),
            sim_seed=_env_int(env, "SIM_SEED", settings.stt_sim_seed),
            sim_latency=_env_bool(env, "SIM_LATENCY", True),
            aws_region=env.get(ENV_PREFIX + "AWS_REGION", "ap-northeast-2"),
            fetch_audio=_env_bool(env, "FETCH_AUDIO", provider == "aws"),
            lease_ms=_env_int(env, "LEASE_MS", keys.TTL_STT_OWNER_MS),
            autoclaim_idle_ms=_env_int(env, "AUTOCLAIM_IDLE_MS", 60_000),
            http_port=_env_int(env, "WORKER_PORT", settings.stt_worker_port),
        )


def build_adapter(cfg: SttWorkerConfig) -> SttAdapter:
    """``simulator`` → :class:`ScriptedSimulator`; ``slow`` → ``SlowStt(simulator, delay_ms)`` (scenario
    B); ``aws`` → :class:`AwsTranscribeStreaming` (SDK required, never exercised offline)."""
    if cfg.provider == "aws":
        from chartwire.stt.aws_transcribe import AwsTranscribeStreaming

        return AwsTranscribeStreaming(cfg.aws_region)
    from chartwire.stt.simulator import ScriptedSimulator, gaussian_latency_ms

    simulator = ScriptedSimulator(
        cfg.scripts_dir, seed=cfg.sim_seed, latency=gaussian_latency_ms if cfg.sim_latency else None
    )
    if cfg.provider == "slow":
        from chartwire.stt.slow import SlowStt

        return SlowStt(simulator, cfg.slow_delay_ms)
    return simulator


def worker_id(settings: Settings) -> str:
    return f"stt:{settings.node_id}:{socket.gethostname()}:{os.getpid()}"


class SessionResolver:
    """``stt:active`` holds session ids only; ``sessions`` is RLS-protected, so the tenant must be
    known before the row can be read. The ``sess:{sid}`` hash field ``tenant`` is used when present,
    otherwise the active tenants are probed (cached ``tenant_cache_s``)."""

    def __init__(self, deps: Any, *, tenant_cache_s: float = 30.0) -> None:
        self.deps = deps
        self._tenants: list[UUID] = []
        self._tenants_at = -1e9
        self._cache_s = tenant_cache_s

    async def _tenants_cached(self, *, refresh: bool = False) -> list[UUID]:
        mono = self.deps.clock.monotonic()
        if refresh or mono - self._tenants_at >= self._cache_s:
            self._tenants = await active_tenant_ids(self.deps.engine)
            self._tenants_at = mono
        return self._tenants

    async def resolve(self, sid: UUID) -> SessionFacts | None:
        hinted = await self.deps.redis.hget(keys.sess(sid), "tenant")
        candidates: list[UUID] = []
        if hinted:
            with contextlib.suppress(ValueError):
                candidates.append(UUID(hinted))
        candidates += [t for t in await self._tenants_cached() if t not in candidates]
        facts = await self._probe(sid, candidates)
        if facts is None:  # a tenant created after the cache was filled
            fresh = [t for t in await self._tenants_cached(refresh=True) if t not in candidates]
            facts = await self._probe(sid, fresh)
        return facts

    async def _probe(self, sid: UUID, tenants: list[UUID]) -> SessionFacts | None:
        for tenant_id in tenants:
            async with tenant_tx(self.deps.engine, TenantCtx.service(tenant_id)) as s:
                row = await sessions_repo.get_session(s, sid)
                if row is None:
                    continue
                tenant = await s.get(Tenant, tenant_id)
                if tenant is None:
                    return None
                started = row.started_at or row.created_at
                if started.tzinfo is None:
                    started = started.replace(tzinfo=UTC)
                return SessionFacts(
                    tenant_id=tenant_id,
                    session_id=row.id,
                    patient_id=row.patient_id,
                    script_ref=row.script_ref,
                    chunk_ms=int(row.chunk_ms),
                    started_at=started,
                    kek_ref=tenant.kek_ref,
                    dek_wrapped=row.dek_wrapped,
                    provider=row.stt_provider,
                    epoch=int(row.epoch),
                )
        return None


class SttWorker:
    """Discovery + ownership + one consumer task per owned session."""

    def __init__(
        self,
        deps: Any,
        adapter: SttAdapter,
        cfg: SttWorkerConfig,
        *,
        worker_id: str,
        drainer: Drainer | None = None,
    ) -> None:
        self.deps = deps
        self.adapter = adapter
        self.cfg = cfg
        self.worker_id = worker_id
        self.drainer = drainer or Drainer(deadline_s=DEFAULT_DEADLINE_S)
        self.resolver = SessionResolver(deps)
        self.consumers: dict[UUID, SessionConsumer] = {}
        self.tasks: dict[UUID, asyncio.Task[str]] = {}
        self.outcomes: dict[UUID, str] = {}
        self.finished: dict[UUID, ConsumerStats] = {}
        """Counters of consumers that already returned (dedup/rebuild evidence for tests and logs)."""
        self._renew = deps.redis.register_script(_RENEW_LUA)
        self._release = deps.redis.register_script(_RELEASE_LUA)
        self._last_renew = deps.clock.monotonic()
        self._stopped = asyncio.Event()

    # --- lease -------------------------------------------------------------------------------------

    async def acquire(self, sid: UUID) -> bool:
        return bool(
            await self.deps.redis.set(keys.stt_owner(sid), self.worker_id, nx=True, px=self.cfg.lease_ms)
        )

    async def owns(self, sid: UUID) -> bool:
        """True while this worker holds the lease. A *missing* key is re-taken on the spot only while the
        session is still active (Redis flushed mid-session: nobody else owns it and the rebuild must go
        on); a lease released because the session finished stays gone."""
        redis = self.deps.redis
        value = await redis.get(keys.stt_owner(sid))
        if value is None:
            if not await redis.sismember(keys.STT_ACTIVE, str(sid)):
                return False
            return await self.acquire(sid)
        return bool(value == self.worker_id)

    async def renew(self, sid: UUID) -> bool:
        result = await self._renew(
            keys=[keys.stt_owner(sid), keys.STT_ACTIVE], args=[self.worker_id, self.cfg.lease_ms, str(sid)]
        )
        return int(result) == 1

    async def release(self, sid: UUID) -> bool:
        return int(await self._release(keys=[keys.stt_owner(sid)], args=[self.worker_id])) == 1

    # --- discovery ---------------------------------------------------------------------------------

    async def run_once(self) -> list[UUID]:
        """One discovery pass: acquire every active session nobody owns; returns the sessions started."""
        started: list[UUID] = []
        raw = await self.deps.redis.smembers(keys.STT_ACTIVE)
        for value in sorted(raw):
            try:
                sid = UUID(value)
            except ValueError:
                log.warning("stt:active member is not a uuid; removing", extra={"member_len": len(value)})
                await self.deps.redis.srem(keys.STT_ACTIVE, value)
                continue
            if sid in self.tasks or not await self.acquire(sid):
                continue
            facts = await self.resolver.resolve(sid)
            if facts is None:
                log.warning(
                    "active session not found in any tenant; dropping", extra={"session_id": str(sid)}
                )
                await self.deps.redis.srem(keys.STT_ACTIVE, value)
                await self.release(sid)
                continue
            self._start(facts)
            started.append(sid)
        return started

    def _start(self, facts: SessionFacts) -> None:
        sid = facts.session_id
        consumer = SessionConsumer(
            self.deps,
            self.adapter,
            facts,
            ConsumerConfig(
                consumer_name=self.worker_id,
                read_count=self.cfg.read_count,
                block_ms=self.cfg.block_ms,
                autoclaim_idle_ms=self.cfg.autoclaim_idle_ms,
                rebuild_wait_s=self.cfg.rebuild_wait_s,
                fetch_audio=self.cfg.fetch_audio,
            ),
            owns=lambda: self.owns(sid),
        )
        self.consumers[sid] = consumer
        task = asyncio.create_task(self._serve(sid, consumer), name=f"stt:{sid}")
        self.tasks[sid] = task
        log.info("session acquired", extra={"session_id": str(sid), "worker_id": self.worker_id})

    async def _serve(self, sid: UUID, consumer: SessionConsumer) -> str:
        outcome = "cancelled"
        try:
            async with self.drainer.track():
                outcome = await consumer.run()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("session consumer crashed", extra={"session_id": str(sid)})
            outcome = "crashed"
        finally:
            with contextlib.suppress(Exception):
                await self.release(sid)
            self.outcomes[sid] = outcome
            self.finished[sid] = consumer.stats
            self.tasks.pop(sid, None)
            self.consumers.pop(sid, None)
        return outcome

    async def _reap(self) -> None:
        """Defensive sweep: a task that finished without running its ``finally`` (loop teardown)."""
        for sid, task in list(self.tasks.items()):
            if task.done():
                self.tasks.pop(sid, None)
                self.consumers.pop(sid, None)
                self.outcomes.setdefault(sid, "cancelled")

    async def _renew_all(self) -> None:
        for sid, consumer in list(self.consumers.items()):
            if await self.renew(sid):
                continue
            log.warning("owner lease lost; stopping consumer", extra={"session_id": str(sid)})
            consumer.stop()
        metrics.STT_LAG_CHUNKS.set(sum(c.stats.lag for c in self.consumers.values()))

    async def run(self) -> None:
        """Discovery loop until the drainer starts; then stop consumers and release the leases."""
        try:
            while self.drainer.accepting:
                await self._reap()
                await self.run_once()
                if self.deps.clock.monotonic() - self._last_renew >= self.cfg.renew_every_s:
                    self._last_renew = self.deps.clock.monotonic()
                    await self._renew_all()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self.drainer.wait_draining(), timeout=self.cfg.discovery_s)
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        for consumer in list(self.consumers.values()):
            consumer.stop()
        pending = [t for t in self.tasks.values() if not t.done()]
        if pending:
            await asyncio.wait(pending, timeout=self.drainer.remaining_s() or DEFAULT_DEADLINE_S)
        for sid, task in list(self.tasks.items()):
            if not task.done():
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task
            self.outcomes.setdefault(sid, "cancelled")
        await self._reap()
        self._stopped.set()

    async def wait_stopped(self) -> None:
        await self._stopped.wait()


# --- process entry ---------------------------------------------------------------------------------


class _OpsServer(uvicorn.Server):
    @contextlib.contextmanager
    def capture_signals(self):  # type: ignore[no-untyped-def]  # the Drainer owns the signals
        yield


async def _serve_ops(
    deps: Any, port: int, drainer: Drainer, health: Health
) -> tuple[_OpsServer, asyncio.Task[None]]:
    app = standalone_app(deps, role="stt-worker", drainer=drainer, health=health)
    server = _OpsServer(uvicorn.Config(app, host="0.0.0.0", port=port, log_level="warning", lifespan="off"))
    task = asyncio.create_task(server.serve(), name="stt-ops-http")
    while not server.started and not task.done():  # noqa: ASYNC110 - uvicorn exposes a flag, not an event
        await asyncio.sleep(0.05)
    if task.done():
        task.result()
    return server, task


async def run(
    settings: Settings,
    *,
    cfg: SttWorkerConfig | None = None,
    ctx: Any = None,
    adapter: SttAdapter | None = None,
    drainer: Drainer | None = None,
    install_signals: bool = True,
    ready: asyncio.Event | None = None,
) -> int:
    """Run until SIGTERM/SIGINT (or ``drainer.begin()``); 0 = every consumer stopped within the budget."""
    cfg = cfg or SttWorkerConfig.from_env(settings)
    own_ctx = ctx is None
    ctx = ctx or build_context(settings)
    drainer = drainer or Drainer(deadline_s=DEFAULT_DEADLINE_S)
    health = build_health(ctx, role="stt-worker")
    drainer.on_begin(health.mark_draining)
    if install_signals:
        chain_signals(drainer, chain=False)
    worker = SttWorker(
        ctx, adapter or build_adapter(cfg), cfg, worker_id=worker_id(settings), drainer=drainer
    )
    http = await _serve_ops(ctx, cfg.http_port, drainer, health) if cfg.http_port else None
    log.info(
        "stt-worker started",
        extra={"worker_id": worker.worker_id, "provider": cfg.provider, "http_port": cfg.http_port},
    )
    task = asyncio.create_task(worker.run(), name="stt-worker")
    if ready is not None:
        ready.set()
    try:
        await drainer.wait_draining()
        clean = await drainer.wait_drained()
        await worker.wait_stopped()
    finally:
        task.cancel()
        with contextlib.suppress(BaseException):
            await task
        if http is not None:
            http[0].should_exit = True
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(http[1], timeout=5.0)
        drainer.uninstall()
        if own_ctx:
            await close_context(ctx)
    log.info(
        "stt-worker stopped",
        extra={"clean": clean, "reason": drainer.reason, "sessions": len(worker.outcomes)},
    )
    return 0 if clean else 1


def main(settings: Settings) -> int:
    """``chartwire serve stt-worker`` entry point (WP-G's CLI calls this)."""
    return asyncio.run(run(settings))


def _cli() -> int:
    from chartwire.core.config import get_settings
    from chartwire.core.logging import configure

    configure(os.environ.get("CHARTWIRE_LOG_LEVEL", "INFO"))
    return main(get_settings())


if __name__ == "__main__":
    sys.exit(_cli())

__all__ = [
    "SessionResolver",
    "SttWorker",
    "SttWorkerConfig",
    "build_adapter",
    "main",
    "run",
    "worker_id",
]
_ = replace  # dataclasses.replace re-exported for tests that derive configs
