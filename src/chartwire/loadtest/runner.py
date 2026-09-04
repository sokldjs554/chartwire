"""Scenario runner (spec §11.2): launches api / worker / stt-worker as **separate processes** pinned
with ``taskset``, seeds a load tenant with N ``created`` sessions, drives N recorders + viewers from
this (client) process, samples per-process CPU/RSS with psutil every second, applies the chaos
schedule (scenario D), checks the database afterwards (the ledger is the truth) and writes
``docs/loadtest/<scenario>.json`` (+ ``docs/eval/alert_latency.json`` from A) and ``results.md``.

Topology (§11.2): api on cores 0–1, worker + stt-worker on 2–3 (PostgreSQL and Redis are the
already-running services of the box), the load client on core 3. Everything shares one host, so
every number is "client-confounded" — the report says so in its caption.

What is *not* measured through REST: tickets are minted in-process with
:func:`chartwire.redis.tickets.issue` because ``POST /ws-ticket`` is rate-limited to 30/min per
principal (§5) and a 200-session ramp would trip it; the WebSocket path is the real one.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import random
import re
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import psutil
from sqlalchemy import func, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from chartwire.auth import passwords
from chartwire.consent.scopes import SCOPE_ORDER
from chartwire.consent.service import policy_hash
from chartwire.core.config import Settings
from chartwire.core.ids import uuid7
from chartwire.crypto.blind_index import blind_index
from chartwire.crypto.envelope import Envelope, aad, dek_fingerprint
from chartwire.crypto.kek import LocalKek
from chartwire.db import cli as dbcli
from chartwire.db.engine import make_engine
from chartwire.db.models import AudioChunk, Patient, RiskEvent, SttOffset, TranscriptSegment
from chartwire.db.models import Session as SessionModel
from chartwire.db.repo import patients as patients_repo
from chartwire.db.repo import sessions as sessions_repo
from chartwire.db.repo import tenancy as tenancy_repo
from chartwire.db.tenant import TenantCtx, tenant_tx
from chartwire.loadtest import report as rpt
from chartwire.loadtest.client import (
    Kind,
    RecorderClient,
    RecorderConfig,
    SessionStats,
    ViewerClient,
    ViewerConfig,
    WebsocketsTransport,
)
from chartwire.loadtest.scenarios import (
    CHUNK_MS,
    ChaosEvent,
    Scenario,
    chaos_schedule,
    pick_kill_targets,
    slow_viewer_indexes,
)
from chartwire.redis import keys, tickets
from chartwire.redis.client import get_redis
from chartwire.synth import vocab_ko
from chartwire.synth.cli import write_scripts
from chartwire.synth.scripts import generate_set

log = logging.getLogger(__name__)

LOAD_SLUG = "loadtest"
LOAD_KEK_REF = "local:loadtest"
LOAD_CLINICIAN_EMAIL = "clinician@load.clinic"
PSEUDONYM_BASE = 5000
"""Load patients are ``가상환자-5001…`` so they never collide with the demo tenant's ``0001…0020``."""
ALL_SCOPES: list[str] = [str(s) for s in SCOPE_ORDER]
READY_TIMEOUT_S = 90.0
SHUTDOWN_TIMEOUT_S = 30.0


@dataclass
class RunConfig:
    scenario: Scenario
    sessions: int
    duration_s: int
    out_dir: Path
    seed: int = 42
    api_port: int = 8120
    worker_port: int = 8121
    stt_port: int = 8122
    cores_api: str = "0-1"
    cores_services: str = "2-3"
    cores_client: str = "3"
    pin: bool = True
    ramp_s: float = 2.0
    tail_wait_s: float = 20.0
    drain_wait_s: float = 120.0
    migrate: bool = True
    workdir: Path | None = None
    keep_workdir: bool = False
    eval_dir: Path = Path("docs/eval")
    write_alert_latency: bool = True
    sample_every_s: float = 1.0

    @property
    def api_base(self) -> str:
        return f"http://127.0.0.1:{self.api_port}"


@dataclass
class Seeded:
    tenant_id: UUID
    clinician_id: UUID
    session_ids: list[UUID]
    scripts_dir: Path


@dataclass
class Proc:
    name: str
    popen: subprocess.Popen[bytes]
    ready_url: str
    metrics_url: str
    log: Path
    exit_code: int | None = None


@dataclass
class Sampler:
    """psutil per-process CPU % / RSS once a second (+ Redis stream length max)."""

    procs: dict[str, list[psutil.Process]]
    series: dict[str, list[rpt.ResourceSample]] = field(default_factory=dict)
    stream_len_max: int = 0

    def sample(self, t_s: float) -> None:
        for name, plist in self.procs.items():
            cpu = rss = 0.0
            alive = False
            for p in plist:
                try:
                    with p.oneshot():
                        cpu += p.cpu_percent(None)
                        rss += p.memory_info().rss / 2**20
                    alive = True
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    continue
            if alive:
                self.series.setdefault(name, []).append(rpt.ResourceSample(round(t_s, 1), cpu, rss))


# --------------------------------------------------------------------------- environment / schema


def db_name(url: str) -> str:
    return str(make_url(url).database or "")


def ensure_schema(settings: Settings) -> None:
    """Idempotent: create roles + the database named by ``CHARTWIRE_DATABASE_URL`` and migrate to head."""
    name = db_name(settings.database_url)
    if settings.superuser_url:
        dbcli.bootstrap_roles(
            settings.superuser_url,
            owner_password=settings.owner_password,
            app_password=settings.app_password,
            databases=[name],
        )
    dbcli.upgrade(settings.database_owner_url, "head")


def child_env(settings: Settings, cfg: RunConfig, workdir: Path) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "CHARTWIRE_DATABASE_URL": settings.database_url,
            "CHARTWIRE_DATABASE_OWNER_URL": settings.database_owner_url,
            "CHARTWIRE_REDIS_URL": settings.redis_url,
            "CHARTWIRE_KEK_MASTER": settings.kek_master,
            "CHARTWIRE_JWT_SECRET": settings.jwt_secret,
            "CHARTWIRE_OBJECTSTORE": f"localfs:{workdir / 'objects'}",
            "CHARTWIRE_SCRIPTS_DIR": str(workdir / "scripts"),
            "CHARTWIRE_STT_SCRIPTS_DIR": str(workdir / "scripts"),
            "CHARTWIRE_STT_PROVIDER": cfg.scenario.stt_provider,
            "CHARTWIRE_STT_SLOW_DELAY_MS": str(cfg.scenario.stt_slow_delay_ms or settings.stt_slow_delay_ms),
            "CHARTWIRE_STT_WORKER_PORT": str(cfg.stt_port),
            "CHARTWIRE_WORKER_PORT": str(cfg.worker_port),
            "CHARTWIRE_LOG_LEVEL": "INFO",
            "PYTHONUNBUFFERED": "1",
        }
    )
    return env


# --------------------------------------------------------------------------- seeding


async def seed_load_tenant(settings: Settings, cfg: RunConfig, workdir: Path) -> Seeded:
    """Tenant ``loadtest`` (created once) + clinician + N patients with 4-scope consent + N ``created``
    sessions with a wrapped DEK and ``script_ref`` s01..sNN; scripts are written to the run's dir."""
    kek = LocalKek(settings.kek_master_bytes)
    master = settings.kek_master_bytes
    owner = make_engine(settings.database_owner_url, pool_size=2, max_overflow=0)
    app = make_engine(settings.database_url, pool_size=2, max_overflow=0)
    try:
        factory = async_sessionmaker(owner, expire_on_commit=False)
        async with factory() as s, s.begin():
            tenant = await tenancy_repo.get_tenant_by_slug(s, LOAD_SLUG)
            if tenant is None:
                tenant = await tenancy_repo.create_tenant(
                    s,
                    slug=LOAD_SLUG,
                    name="가상의원 부하테스트",
                    kek_ref=LOAD_KEK_REF,
                    record_key_wrapped=kek.wrap(Envelope.new_dek(), LOAD_KEK_REF),
                    settings={"synthetic": True, "seeded_by": "chartwire loadtest"},
                )
        scripts = generate_set("demo", cfg.sessions, cfg.seed)
        rng = random.Random(f"loadtest:{cfg.seed}")
        async with tenant_tx(app, TenantCtx.service(tenant.id)) as s:
            email_hmac = blind_index(master, tenant.id, LOAD_CLINICIAN_EMAIL)
            clinician = await tenancy_repo.find_user_by_email_hmac(s, tenant.id, email_hmac)
            if clinician is None:
                record_key = kek.unwrap(bytes(tenant.record_key_wrapped), tenant.kek_ref)
                clinician = await tenancy_repo.create_user(
                    s,
                    tenant_id=tenant.id,
                    role="clinician",
                    email_hmac=email_hmac,
                    email_enc=Envelope.encrypt(
                        record_key,
                        LOAD_CLINICIAN_EMAIL.encode(),
                        aad(tenant.id, "user", email_hmac.hex(), "email"),
                    ),
                    display_name="가상임상의(부하)",
                    password_hash=passwords.hash("load1234!"),
                )
            existing = {
                p.pseudonym: p
                for p in (await s.scalars(select(Patient).where(Patient.tenant_id == tenant.id))).all()
            }
            session_ids: list[UUID] = []
            for i, script in enumerate(scripts):
                pseudonym = vocab_ko.pseudonym(PSEUDONYM_BASE + i + 1)
                patient = existing.get(pseudonym)
                if patient is None:
                    dek = Envelope.new_dek()
                    wrapped = kek.wrap(dek, tenant.kek_ref)
                    patient = await patients_repo.create_patient(
                        s,
                        tenant_id=tenant.id,
                        pseudonym=pseudonym,
                        name_enc=Envelope.encrypt(
                            dek, pseudonym.encode(), aad(tenant.id, "patient", pseudonym, "name")
                        ),
                        name_hmac=blind_index(master, tenant.id, pseudonym),
                        birth_year=rng.randint(1958, 2006),
                        sex=rng.choice(("F", "M")),
                        phone_enc=None,
                        dek_wrapped=wrapped,
                        dek_fingerprint=dek_fingerprint(wrapped),
                    )
                    existing[pseudonym] = patient
                active = await patients_repo.latest_active_consent(s, patient.id)
                if active is None or set(active.scopes) < set(ALL_SCOPES):
                    await patients_repo.grant_consent(
                        s,
                        tenant_id=tenant.id,
                        patient_id=patient.id,
                        scopes=ALL_SCOPES,
                        granted_by=clinician.id,
                        channel="loadtest",
                        policy_hash=policy_hash(ALL_SCOPES, "loadtest"),
                    )
                wrapped = kek.wrap(Envelope.new_dek(), tenant.kek_ref)
                row = await sessions_repo.create_session(
                    s,
                    id=uuid7(),
                    tenant_id=tenant.id,
                    patient_id=patient.id,
                    clinician_id=clinician.id,
                    script_ref=script.script_ref,
                    scopes_snapshot=ALL_SCOPES,
                    dek_wrapped=wrapped,
                    dek_fingerprint=dek_fingerprint(wrapped),
                    chunk_ms=CHUNK_MS,
                )
                session_ids.append(row.id)
        scripts_dir = workdir / "scripts"
        write_scripts(scripts, scripts_dir)
        return Seeded(tenant.id, clinician.id, session_ids, scripts_dir)
    finally:
        await owner.dispose()
        await app.dispose()


async def clear_stale_state(settings: Settings, seeded: Seeded, redis: Any) -> int:
    """Evict earlier runs' sessions from ``stt:active`` (and their hot keys). Returns how many.

    This is the harness's own dirt, not a system defect: a session whose recorder never sent ``end``
    stays in ``stt:active`` for ever by design (§7.4 — only the end-marker flush does ``SREM``),
    while its ``audio_chunks`` rows point at objects under a *previous* run's workdir, which the
    runner deletes when that run finishes, and at ``script_ref`` values that only existed in that
    run's script set. A fresh stt-worker therefore acquires them, crashes on the missing script or
    object, releases, re-acquires — and spends its session budget on the dead ones. That is exactly
    what emptied scenario C after A n=200 left 170 sessions behind (``docs/dev/handoff/loadfix.md``).
    Only sessions of the *load* tenant that are not part of this run are touched.
    """
    engine = make_engine(settings.database_url, pool_size=2, max_overflow=0)
    try:
        members = {str(m) for m in await redis.smembers(keys.STT_ACTIVE)}
        current = {str(s) for s in seeded.session_ids}
        candidates = members - current
        if not candidates:
            return 0
        async with tenant_tx(engine, TenantCtx.service(seeded.tenant_id)) as s:
            rows = await s.scalars(select(SessionModel.id).where(SessionModel.tenant_id == seeded.tenant_id))
            stale = sorted(candidates & {str(r) for r in rows.all()})
        if not stale:
            return 0
        async with redis.pipeline(transaction=False) as pipe:
            for sid in stale:
                pipe.srem(keys.STT_ACTIVE, sid)
                pipe.delete(keys.sess(sid), keys.sess_chunks(sid), keys.sess_viewers(sid))
                pipe.delete(keys.stt_owner(sid))
                pipe.hdel(keys.STT_LAG, sid)
            await pipe.execute()
        log.warning(
            "evicted stale loadtest sessions from stt:active",
            extra={"stale": len(stale), "active_before": len(members)},
        )
        return len(stale)
    finally:
        await engine.dispose()


# --------------------------------------------------------------------------- processes


def _taskset(cores: str | None, pin: bool) -> list[str]:
    if not pin or not cores or shutil.which("taskset") is None:
        return []
    return ["taskset", "-c", cores]


def spawn_processes(settings: Settings, cfg: RunConfig, workdir: Path) -> list[Proc]:
    env = child_env(settings, cfg, workdir)
    logs = workdir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    specs = [
        (
            "api",
            cfg.cores_api,
            [
                sys.executable,
                "-m",
                "chartwire.cli",
                "serve",
                "api",
                "--host",
                "127.0.0.1",
                "--port",
                str(cfg.api_port),
                "--log-level",
                "WARNING",
            ],
            cfg.api_port,
            "load-api",
        ),
        (
            "worker",
            cfg.cores_services,
            [sys.executable, "-m", "chartwire.cli", "serve", "worker", "--log-level", "WARNING"],
            cfg.worker_port,
            "load-worker",
        ),
        (
            "stt-worker",
            cfg.cores_services,
            [sys.executable, "-m", "chartwire.stt.worker"],
            cfg.stt_port,
            "load-stt",
        ),
    ]
    procs: list[Proc] = []
    for name, cores, argv, port, node_id in specs:
        log_path = logs / f"{name}.log"
        popen = subprocess.Popen(
            [*_taskset(cores, cfg.pin), *argv],
            env={**env, "CHARTWIRE_NODE_ID": node_id},
            stdout=log_path.open("wb"),
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        procs.append(
            Proc(
                name,
                popen,
                f"http://127.0.0.1:{port}/readyz",
                f"http://127.0.0.1:{port}/metrics",
                log_path,
            )
        )
    return procs


async def wait_ready(procs: Sequence[Proc], timeout_s: float = READY_TIMEOUT_S) -> None:
    deadline = time.monotonic() + timeout_s
    async with httpx.AsyncClient(timeout=2.0) as http:
        pending = {p.name: p for p in procs}
        while pending and time.monotonic() < deadline:
            for name, proc in list(pending.items()):
                if proc.popen.poll() is not None:
                    raise RuntimeError(f"{name} exited early (code {proc.popen.returncode}); see {proc.log}")
                with contextlib.suppress(httpx.HTTPError):
                    resp = await http.get(proc.ready_url)
                    if resp.status_code == 200:
                        del pending[name]
            if pending:
                await asyncio.sleep(0.5)
    if pending:
        raise TimeoutError(f"not ready after {timeout_s:.0f}s: {sorted(pending)}")


def stop_processes(procs: Sequence[Proc], timeout_s: float = SHUTDOWN_TIMEOUT_S) -> None:
    for p in procs:
        if p.popen.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.kill(p.popen.pid, signal.SIGCONT)  # a SIGSTOPped worker must wake up to drain
                p.popen.send_signal(signal.SIGTERM)
    deadline = time.monotonic() + timeout_s
    for p in procs:
        remaining = max(0.1, deadline - time.monotonic())
        try:
            p.popen.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            p.popen.kill()
            p.popen.wait(timeout=5)
        p.exit_code = p.popen.returncode


def _postgres_processes() -> list[psutil.Process]:
    out: list[psutil.Process] = []
    for p in psutil.process_iter(["name"]):
        with contextlib.suppress(psutil.Error):
            if (p.info["name"] or "").startswith("postgres"):
                out.append(p)
    return out


def _redis_processes() -> list[psutil.Process]:
    out: list[psutil.Process] = []
    for p in psutil.process_iter(["name"]):
        with contextlib.suppress(psutil.Error):
            if (p.info["name"] or "").startswith("redis-server"):
                out.append(p)
    return out


def build_sampler(procs: Sequence[Proc]) -> Sampler:
    tracked: dict[str, list[psutil.Process]] = {p.name: [psutil.Process(p.popen.pid)] for p in procs}
    tracked["postgres"] = _postgres_processes()
    tracked["redis"] = _redis_processes()
    tracked["client"] = [psutil.Process(os.getpid())]
    sampler = Sampler(tracked)
    sampler.sample(0.0)  # prime cpu_percent
    sampler.series.clear()
    return sampler


# --------------------------------------------------------------------------- metrics scrape

_METRIC_RE = re.compile(
    r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)(?P<labels>\{[^}]*\})?\s+(?P<value>[-+0-9.eE]+|NaN)"
)


def parse_metrics(text: str) -> dict[str, float]:
    """``name{labels}`` → value for every sample line (labels kept verbatim; sums by name too)."""
    out: dict[str, float] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        m = _METRIC_RE.match(line)
        if m is None:
            continue
        try:
            value = float(m.group("value"))
        except ValueError:
            continue
        name = m.group("name")
        out[name + (m.group("labels") or "")] = value
        if m.group("labels"):
            out[name] = out.get(name, 0.0) + value
    return out


async def scrape(url: str) -> dict[str, float]:
    try:
        async with httpx.AsyncClient(timeout=5.0) as http:
            resp = await http.get(url)
            resp.raise_for_status()
            return parse_metrics(resp.text)
    except httpx.HTTPError as exc:
        log.warning("metrics scrape failed", extra={"url": url, "error": type(exc).__name__})
        return {}


# --------------------------------------------------------------------------- database check


async def db_check(settings: Settings, seeded: Seeded, sent: dict[str, int]) -> rpt.DbCheck:
    engine: AsyncEngine = make_engine(settings.database_url, pool_size=2, max_overflow=0)
    ids = seeded.session_ids
    check = rpt.DbCheck(sessions=len(ids), chunks_sent=sum(sent.values()))
    try:
        async with tenant_tx(engine, TenantCtx.service(seeded.tenant_id)) as s:
            rows = (
                await s.execute(
                    select(SessionModel.id, SessionModel.state, SessionModel.final_seq).where(
                        SessionModel.id.in_(ids)
                    )
                )
            ).all()
            final_seq = {str(r.id): r.final_seq for r in rows}
            check.sessions_ended = sum(1 for r in rows if r.state in ("ended", "transcribed", "drafted"))
            check.sessions_transcribed = sum(1 for r in rows if r.state in ("transcribed", "drafted"))
            chunk_rows = (
                await s.execute(
                    select(AudioChunk.session_id, func.count(), func.max(AudioChunk.seq))
                    .where(AudioChunk.session_id.in_(ids))
                    .group_by(AudioChunk.session_id)
                )
            ).all()
            by_session = {str(r[0]): int(r[1]) for r in chunk_rows}
            check.chunk_rows = sum(by_session.values())
            for sid in ids:
                key = str(sid)
                check.per_session_loss[key] = max(0, sent.get(key, 0) - by_session.get(key, 0))
            offsets = (
                await s.execute(
                    select(SttOffset.session_id, SttOffset.last_chunk_seq).where(
                        SttOffset.session_id.in_(ids)
                    )
                )
            ).all()
            for sid, last in offsets:
                fs = final_seq.get(str(sid))
                if fs is not None and int(last) == int(fs):
                    check.stt_offsets_complete += 1
            segs = (
                await s.execute(
                    select(TranscriptSegment.session_id, func.count(), func.max(TranscriptSegment.seq))
                    .where(TranscriptSegment.session_id.in_(ids))
                    .group_by(TranscriptSegment.session_id)
                )
            ).all()
            seen = set()
            for sid, count, max_seq in segs:
                seen.add(str(sid))
                check.segment_rows += int(count)
                if int(count) == int(max_seq) + 1:
                    check.segments_contiguous += 1
            check.segments_zero_row = sum(1 for sid in ids if str(sid) not in seen)
            # 0 rows = trivially contiguous — recorded separately so the ratio cannot read as clean
            # for a run that shed sessions before they sent anything (report.DbCheck).
            check.segments_contiguous += check.segments_zero_row
            check.risk_events = int(
                (
                    await s.execute(
                        select(func.count()).select_from(RiskEvent).where(RiskEvent.session_id.in_(ids))
                    )
                ).scalar_one()
            )
    finally:
        await engine.dispose()
    return check


async def pg_version(settings: Settings) -> str | None:
    from sqlalchemy import text

    engine = make_engine(settings.database_url, pool_size=1, max_overflow=0)
    try:
        async with engine.connect() as conn:
            return str((await conn.execute(text("SHOW server_version"))).scalar_one())
    except Exception:
        return None
    finally:
        await engine.dispose()


# --------------------------------------------------------------------------- clients


class RedisTicketSource:
    """In-process ticket minting (same payload the REST route stores; see module docstring)."""

    def __init__(self, redis: Any, *, tenant_id: UUID, user_id: UUID) -> None:
        self._redis = redis
        self._tenant_id = tenant_id
        self._user_id = user_id

    async def __call__(self, session_id: str, kind: Kind) -> str:
        return await tickets.issue(
            self._redis,
            tenant_id=self._tenant_id,
            user_id=self._user_id,
            role="clinician",
            session_id=session_id,
            kind=kind,
        )


def ws_url(api_base: str, kind: str) -> str:
    return f"ws://{api_base.removeprefix('http://')}/ws/v1/{kind}"


@dataclass
class LoadClients:
    stats: list[SessionStats]
    recorders: list[RecorderClient]
    viewers: list[ViewerClient]
    tasks: list[asyncio.Task[Any]] = field(default_factory=list)


def build_clients(cfg: RunConfig, seeded: Seeded, ticket_source: RedisTicketSource) -> LoadClients:
    scenario = cfg.scenario
    slow = slow_viewer_indexes(scenario, cfg.sessions)
    total = scenario.total_chunks(cfg.duration_s)
    out = LoadClients([], [], [])
    for i, sid in enumerate(seeded.session_ids):
        stats = SessionStats(str(sid))
        recorder = RecorderClient(
            RecorderConfig(
                session_id=str(sid),
                ingest_url=ws_url(cfg.api_base, "ingest"),
                total_chunks=total,
                chunk_ms=CHUNK_MS,
                seed=cfg.seed * 1000 + i,
                end_wait_s=15.0,
            ),
            connect=WebsocketsTransport.connect,
            tickets=ticket_source,
            stats=stats,
        )
        out.stats.append(stats)
        out.recorders.append(recorder)
        for _ in range(scenario.viewers_per_session):
            out.viewers.append(
                ViewerClient(
                    ViewerConfig(
                        session_id=str(sid),
                        watch_url=ws_url(cfg.api_base, "watch"),
                        chunk_ms=CHUNK_MS,
                        per_message_delay_s=scenario.slow_viewer_delay_s if i in slow else 0.0,
                        stop_on_bye_ended=False,
                    ),
                    connect=WebsocketsTransport.connect,
                    tickets=ticket_source,
                    stats=stats,
                )
            )
    return out


async def _start_clients(clients: LoadClients, ramp_s: float) -> None:
    n = len(clients.recorders)
    viewers_per = len(clients.viewers) // max(n, 1)
    for i, recorder in enumerate(clients.recorders):
        for v in clients.viewers[i * viewers_per : (i + 1) * viewers_per]:
            clients.tasks.append(asyncio.create_task(v.run(), name=f"viewer-{i}"))
        clients.tasks.append(asyncio.create_task(recorder.run(), name=f"recorder-{i}"))
        if ramp_s > 0 and n > 1:
            await asyncio.sleep(ramp_s / n)


# --------------------------------------------------------------------------- chaos


async def run_chaos(
    events: Sequence[ChaosEvent],
    clients: LoadClients,
    procs: Sequence[Proc],
    redis: Any,
    rng: random.Random,
    t0: float,
    journal: list[dict[str, Any]],
) -> None:
    stt = next((p for p in procs if p.name == "stt-worker"), None)
    session_ids = [r.cfg.session_id for r in clients.recorders]
    by_id = {r.cfg.session_id: r for r in clients.recorders}
    for ev in events:
        delay = ev.at_s - (time.monotonic() - t0)
        if delay > 0:
            await asyncio.sleep(delay)
        entry: dict[str, Any] = {"kind": ev.kind, "at_s": round(time.monotonic() - t0, 2)}
        if ev.kind == "kill_sockets":
            targets = pick_kill_targets(rng, session_ids, ev.fraction)
            for sid in targets:
                by_id[sid].abort_connection()
            entry["targets"] = len(targets)
        elif ev.kind == "flush_redis":
            # spec §11.2 says FLUSHALL; the box is shared, so only the run's own database is flushed —
            # identical from the app's point of view (it only uses this index).
            await redis.flushdb()
            entry["targets"] = "FLUSHDB"
        elif ev.kind == "stt_sigstop" and stt is not None:
            os.kill(stt.popen.pid, signal.SIGSTOP)
            entry["targets"] = stt.popen.pid
        elif ev.kind == "stt_sigcont" and stt is not None:
            os.kill(stt.popen.pid, signal.SIGCONT)
            entry["targets"] = stt.popen.pid
        journal.append(entry)
        log.info("chaos", extra=entry)


# --------------------------------------------------------------------------- run


async def _sampling_loop(
    sampler: Sampler, redis: Any, session_ids: Sequence[str], every_s: float, t0: float
) -> None:
    while True:
        await asyncio.sleep(every_s)
        sampler.sample(time.monotonic() - t0)
        with contextlib.suppress(Exception):
            pipe = redis.pipeline(transaction=False)
            for sid in session_ids:
                pipe.xlen(keys.sess_chunks(sid))
            lengths = await pipe.execute()
            sampler.stream_len_max = max(sampler.stream_len_max, max((int(x) for x in lengths), default=0))


def _cancel(tasks: Sequence[asyncio.Task[Any]]) -> None:
    for t in tasks:
        if not t.done():
            t.cancel()


async def _wait_recorders(clients: LoadClients, deadline_s: float) -> None:
    rec = [t for t in clients.tasks if t.get_name().startswith("recorder-")]
    _done, pending = await asyncio.wait(rec, timeout=deadline_s)
    for t in pending:  # scenario B: credit → 0 keeps the recorder from finishing in time — stop it
        idx = int(t.get_name().split("-")[1])
        clients.recorders[idx].stop("duration_exceeded")
    if pending:
        await asyncio.wait(pending, timeout=10.0)


async def wait_stt_drain(
    settings: Settings, seeded: Seeded, bound_s: float, *, stall_s: float = 30.0
) -> tuple[bool, float]:
    """Wait until the stt pipeline has caught up with every session that ended — i.e. no session of
    this run is still sitting in state ``ended`` — and report ``(drained, waited_s)``.

    §11.2 calls ``stt_offsets.last_chunk_seq == final_seq`` a **final** invariant, but the check used
    to run a fixed 20 s after the last recorder. That turns the invariant into a race against the
    stt-worker's backlog, which is why the same healthy system reported 100 % (A n=50/100), 70 %
    (D, after a 15 s SIGSTOP) and 0 % (B, where ``SlowStt`` is 400 ms per chunk *by design*) — the
    metric and the system were both right and the harness was reading them too early.

    Bounded twice: ``bound_s`` overall, and ``stall_s`` without progress — a run whose worker is
    saturated (A n=200) must not sit here for the whole bound. Viewers are still connected, so the
    finals that arrive during the wait are still measured.

    Progress is the **sum of ``stt_offsets.last_chunk_seq``**, not the number of sessions still
    ``ended``: in scenario B every session finishes recording within a second of the others, so the
    session count sits flat at N until the very end and a count-based stall detector gives up on a
    pipeline that is chewing through its backlog at full speed. The offsets sum advances per chunk.
    """
    engine = make_engine(settings.database_url, pool_size=2, max_overflow=0)
    started = time.monotonic()
    deadline, best, best_at = started + bound_s, -1, started
    try:
        while time.monotonic() < deadline:
            async with tenant_tx(engine, TenantCtx.service(seeded.tenant_id)) as s:
                pending = int(
                    await s.scalar(
                        select(func.count())
                        .select_from(SessionModel)
                        .where(SessionModel.id.in_(seeded.session_ids), SessionModel.state == "ended")
                    )
                    or 0
                )
                progress = int(
                    await s.scalar(
                        select(func.coalesce(func.sum(SttOffset.last_chunk_seq), 0)).where(
                            SttOffset.session_id.in_(seeded.session_ids)
                        )
                    )
                    or 0
                )
            if pending == 0:
                return True, time.monotonic() - started
            if progress > best:
                best, best_at = progress, time.monotonic()
            elif time.monotonic() - best_at >= stall_s:
                log.warning(
                    "stt drain stalled",
                    extra={"pending": pending, "chunks_transcribed": progress, "stall_s": stall_s},
                )
                break
            await asyncio.sleep(2.0)
    finally:
        await engine.dispose()
    return False, time.monotonic() - started


async def _wait_viewers_tail(clients: LoadClients, tail_wait_s: float) -> None:
    """Give the stt-worker time to finish after the last ``bye{ended}``: wait until every viewer saw
    ``session.state{transcribed}`` (or ``ended`` for sessions the recorder stopped early), else the tail."""
    deadline = time.monotonic() + tail_wait_s
    while time.monotonic() < deadline:
        if all("transcribed" in s.states or s.outcome != "ended" for s in clients.stats):
            break
        await asyncio.sleep(0.5)
    for v in clients.viewers:
        v.stop()
    vt = [t for t in clients.tasks if t.get_name().startswith("viewer-")]
    if vt:
        await asyncio.wait(vt, timeout=5.0)
    _cancel(clients.tasks)
    await asyncio.gather(*clients.tasks, return_exceptions=True)


async def run(settings: Settings, cfg: RunConfig) -> dict[str, Any]:
    scenario = cfg.scenario
    workdir = cfg.workdir or Path(os.environ.get("CHARTWIRE_LOADTEST_WORKDIR", "var/loadtest")) / (
        f"{scenario.name}-n{cfg.sessions}-{int(time.time())}"
    )
    workdir.mkdir(parents=True, exist_ok=True)
    if cfg.migrate:
        ensure_schema(settings)
    seeded = await seed_load_tenant(settings, cfg, workdir)
    log.info(
        "loadtest seeded",
        extra={"scenario": scenario.name, "sessions": len(seeded.session_ids), "workdir": str(workdir)},
    )
    redis = get_redis(settings.redis_url)
    stale_evicted = await clear_stale_state(settings, seeded, redis)
    procs = spawn_processes(settings, cfg, workdir)
    journal: list[dict[str, Any]] = []
    background: list[asyncio.Task[Any]] = []
    try:
        await wait_ready(procs)
        if cfg.pin and cfg.cores_client:
            with contextlib.suppress(OSError, ValueError):
                os.sched_setaffinity(0, _parse_cores(cfg.cores_client))
        sampler = build_sampler(procs)
        ticket_source = RedisTicketSource(redis, tenant_id=seeded.tenant_id, user_id=seeded.clinician_id)
        clients = build_clients(cfg, seeded, ticket_source)
        session_ids = [str(s) for s in seeded.session_ids]
        t0 = time.monotonic()
        background.append(
            asyncio.create_task(_sampling_loop(sampler, redis, session_ids, cfg.sample_every_s, t0))
        )
        if scenario.chaos:
            background.append(
                asyncio.create_task(
                    run_chaos(
                        chaos_schedule(scenario, cfg.duration_s),
                        clients,
                        procs,
                        redis,
                        random.Random(cfg.seed),
                        t0,
                        journal,
                    )
                )
            )
        await _start_clients(clients, cfg.ramp_s)
        await _wait_recorders(clients, deadline_s=cfg.duration_s + cfg.ramp_s + 30.0)
        recorders_done_s = time.monotonic() - t0
        stt_drained, stt_drain_wait_s = await wait_stt_drain(settings, seeded, cfg.drain_wait_s)
        await _wait_viewers_tail(clients, cfg.tail_wait_s)
        wall_s = time.monotonic() - t0
        _cancel(background)
        await asyncio.gather(*background, return_exceptions=True)
        api_metrics = await scrape(next(p.metrics_url for p in procs if p.name == "api"))
        stt_metrics = await scrape(next(p.metrics_url for p in procs if p.name == "stt-worker"))
        sent = {s.session_id: s.sent for s in clients.stats}
        db_check_at_s = time.monotonic() - t0
        check = await db_check(settings, seeded, sent)
        version = await pg_version(settings)
    finally:
        _cancel(background)
        stop_processes(procs)
        with contextlib.suppress(Exception):
            await redis.aclose()
    agg = rpt.aggregate(clients.stats, duration_s=cfg.duration_s, ramp_s=cfg.ramp_s)
    agg["recorders_done_s"] = round(recorders_done_s, 1)
    agg["wall_s"] = round(wall_s, 1)
    resources = rpt.summarize_resources(sampler.series)
    common = {
        "scenario": scenario.name,
        "description": scenario.description,
        "sessions": cfg.sessions,
        "duration_s": cfg.duration_s,
        "chunk_ms": CHUNK_MS,
        "stt_provider": scenario.stt_provider,
        "stt_slow_delay_ms": scenario.stt_slow_delay_ms,
        "pinning": {"api": cfg.cores_api, "services": cfg.cores_services, "client": cfg.cores_client}
        if cfg.pin
        else None,
        "process_exit_codes": {p.name: p.exit_code for p in procs},
        "workdir": str(workdir),
        "database": db_name(settings.database_url),
        "stale_sessions_evicted": stale_evicted,
        "db_check_at_s": round(db_check_at_s, 1),
        "stt_drained": stt_drained,
        "stt_drain_wait_s": round(stt_drain_wait_s, 1),
        "metrics": {
            "ws_dropped_partials_total": api_metrics.get("ws_dropped_partials_total"),
            "ws_resume_total_ok": api_metrics.get('ws_resume_total{result="ok"}'),
            "ws_resume_total_gap": api_metrics.get('ws_resume_total{result="gap_unrecoverable"}'),
            "stt_rebuilds_total": stt_metrics.get("stt_rebuilds_total"),
            "stt_lag_chunks": stt_metrics.get("stt_lag_chunks"),
        },
    }
    report = _build_report(cfg, agg, resources, check, sampler, common, journal, version, settings)
    rpt.write_json(cfg.out_dir / f"{scenario.name}.json", report)
    if scenario.name == "A" and cfg.write_alert_latency:
        body = rpt.alert_latency_body(agg, n_run=cfg.sessions)
        if body is not None:
            rpt.write_json(
                cfg.eval_dir / "alert_latency.json", rpt.finish(cfg.seed, body, pg_version=version)
            )
    write_results_md(cfg.out_dir)
    if not cfg.keep_workdir:
        shutil.rmtree(workdir / "objects", ignore_errors=True)
    return report


def _build_report(
    cfg: RunConfig,
    agg: dict[str, Any],
    resources: dict[str, Any],
    check: rpt.DbCheck,
    sampler: Sampler,
    common: dict[str, Any],
    journal: list[dict[str, Any]],
    version: str | None,
    settings: Settings,
) -> dict[str, Any]:
    name = cfg.scenario.name
    out = cfg.out_dir
    if name == "A":
        existing = rpt.read_json(out / "A.json")
        run_entry = rpt.a_run(cfg.sessions, agg, resources, check, metrics=common["metrics"])
        run_entry.update(
            {
                k: common[k]
                for k in (
                    "process_exit_codes",
                    "workdir",
                    "pinning",
                    "database",
                    "stale_sessions_evicted",
                    "db_check_at_s",
                    "stt_drained",
                    "stt_drain_wait_s",
                )
            }
        )
        body: dict[str, Any] = {
            "scenario": "A",
            "description": common["description"],
            "chunk_ms": CHUNK_MS,
            "caption": rpt.LOAD_CAPTION,
            "runs": rpt.merge_a_runs(existing, run_entry),
        }
    elif name == "B":
        body = {
            **common,
            **rpt.b_body(
                agg,
                resources,
                check,
                stream_len_max=sampler.stream_len_max,
                stream_maxlen=settings.stream_maxlen,
            ),
        }
    elif name == "C":
        a = rpt.read_json(out / "A.json") or {}
        ref = next(
            (
                r.get("ack_p95_ms")
                for r in a.get("runs", [])
                if isinstance(r, dict) and r.get("n") == cfg.sessions
            ),
            None,
        )
        dropped = common["metrics"].get("ws_dropped_partials_total")
        body = {
            **common,
            **rpt.c_body(
                agg,
                resources,
                check,
                reference_ack_p95_ms=ref,
                dropped_partials=None if dropped is None else int(dropped),
                slow_viewers=cfg.scenario.slow_viewers(cfg.sessions),
            ),
        }
    elif name == "D":
        rebuilds = common["metrics"].get("stt_rebuilds_total")
        body = {
            **common,
            **rpt.d_body(
                agg,
                resources,
                check,
                rebuild_count=None if rebuilds is None else int(rebuilds),
                chaos_log=journal,
            ),
        }
    else:  # pragma: no cover - the CLI only passes A-D here
        body = {**common, "clients": agg, "resources": resources, "db": check.as_dict()}
    return rpt.finish(cfg.seed, body, pg_version=version)


def write_results_md(out_dir: Path) -> Path:
    path = out_dir / "results.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(rpt.render_results_md(rpt.load_reports(out_dir)), encoding="utf-8")
    return path


def _parse_cores(spec: str) -> set[int]:
    cores: set[int] = set()
    for part in spec.split(","):
        if "-" in part:
            lo, hi = part.split("-", 1)
            cores.update(range(int(lo), int(hi) + 1))
        elif part.strip():
            cores.add(int(part))
    return cores
