"""Live-server harness for the WebSocket e2e tests: a real uvicorn on port 8101 (thread) serving
``chartwire.ws.routes`` with the same ``AppDeps`` shape WP-E's ``create_app`` builds, plus a minimal
protocol-compliant recorder client (credit rule, nack re-sends, pong, resume)."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import random
import threading
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import orjson
import uvicorn
import websockets
from fastapi import FastAPI
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from chartwire.core.clock import SystemClock
from chartwire.core.config import Settings
from chartwire.crypto import Envelope, KeyCache, LocalKek, dek_fingerprint
from chartwire.db.engine import make_engine
from chartwire.db.repo import patients as patients_repo
from chartwire.db.repo import sessions as sessions_repo
from chartwire.db.tenant import TenantCtx, tenant_tx
from chartwire.objectstore import from_spec
from chartwire.redis import tickets
from chartwire.ws import routes
from chartwire.ws.codec import FLAG_SIM, FrameHeader, encode_frame

CHUNK_BYTES = 6_400


def build_app(settings: Settings) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = make_engine(settings.database_url, pool_size=5)
        redis = Redis.from_url(settings.redis_url, decode_responses=True)
        kek = LocalKek(settings.kek_master_bytes)
        app.state.deps = SimpleNamespace(
            settings=settings,
            engine=engine,
            redis=redis,
            kek=kek,
            keycache=KeyCache(kek),
            objectstore=from_spec(settings.objectstore),
            clock=SystemClock(),
            node_id=settings.node_id,
        )
        app.state.loop = asyncio.get_running_loop()  # tests poke the runtime with call_soon_threadsafe
        await routes.on_startup(app, app.state.deps)
        try:
            yield
        finally:
            await routes.on_shutdown(app)
            await engine.dispose()
            await redis.aclose()

    app = FastAPI(lifespan=lifespan)
    app.include_router(routes.router)
    return app


@dataclass
class LiveServer:
    settings: Settings
    port: int
    _server: uvicorn.Server | None = None
    _thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}"

    @property
    def app(self) -> FastAPI:
        assert self._server is not None
        return self._server.config.app  # type: ignore[return-value]

    def start(self) -> None:
        config = uvicorn.Config(
            build_app(self.settings), host="127.0.0.1", port=self.port, log_level="warning", lifespan="on"
        )
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, name="live-uvicorn", daemon=True)
        self._thread.start()
        deadline = time.monotonic() + 15
        while not self._server.started:
            if time.monotonic() > deadline or not self._thread.is_alive():
                raise RuntimeError("live server did not start")
            time.sleep(0.05)

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=30)


@contextlib.contextmanager
def live_server(
    db_url: str, redis_url: str, objects_dir: Path, *, port: int, node_id: str = "node-test"
) -> Iterator[LiveServer]:
    settings = Settings(
        database_url=db_url, redis_url=redis_url, objectstore=f"localfs:{objects_dir}", node_id=node_id
    )
    server = LiveServer(settings, port)
    server.start()
    try:
        yield server
    finally:
        server.stop()


# --- seeding ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Seeded:
    tenant_id: UUID
    clinician_id: UUID
    patient_id: UUID
    session_id: UUID
    kek_ref: str
    dek: bytes


async def seed_session(
    factories: Any,
    app_engine: AsyncEngine,
    settings: Settings,
    *,
    consent: bool = True,
    slug: str = "clinic-a",
) -> Seeded:
    """Tenant + clinician + patient (+ consent) + recording session with a real wrapped DEK."""
    tenant = await factories.tenant(slug)
    clinician = await factories.user(tenant.id, "clinician")
    patient = await factories.patient(tenant.id)
    session = await factories.session(tenant.id, patient.id, clinician.id)
    dek = Envelope.new_dek()
    wrapped = LocalKek(settings.kek_master_bytes).wrap(dek, tenant.kek_ref)
    async with tenant_tx(app_engine, TenantCtx.service(tenant.id)) as s:
        await sessions_repo.update_session(
            s, session.id, dek_wrapped=wrapped, dek_fingerprint=dek_fingerprint(wrapped)
        )
        if consent:
            await patients_repo.grant_consent(
                s,
                tenant_id=tenant.id,
                patient_id=patient.id,
                scopes=["recording", "transcription"],
                granted_by=clinician.id,
            )
    return Seeded(tenant.id, clinician.id, patient.id, session.id, tenant.kek_ref, dek)


async def ingest_ticket(
    redis: Redis, seeded: Seeded, kind: str = "ingest", session_id: UUID | None = None
) -> str:
    return await tickets.issue(
        redis,
        tenant_id=seeded.tenant_id,
        user_id=seeded.clinician_id,
        role="clinician",
        session_id=session_id or seeded.session_id,
        kind=kind,  # type: ignore[arg-type]
    )


def payload_for(seq: int, seed: int = 0) -> bytes:
    return random.Random(seed * 1_000_003 + seq).randbytes(CHUNK_BYTES)


def frame(seq: int, seed: int = 0, *, flags: int = FLAG_SIM) -> bytes:
    return encode_frame(FrameHeader(seq=seq, offset_ms=(seq - 1) * 200, flags=flags), payload_for(seq, seed))


def sha_of(seq: int, seed: int = 0) -> bytes:
    return hashlib.sha256(payload_for(seq, seed)).digest()


# --- recorder client ----------------------------------------------------------------------------------


def dumps(msg: dict[str, Any]) -> str:
    return orjson.dumps(msg).decode()


class Recorder:
    """A compliant recorder: ``last_sent − ack_seq ≤ credit`` at send time, re-sends nacked ranges from
    its ring buffer, answers pings, and stops on ``bye``. ``kill_at`` aborts the TCP transport right after
    sending that seq (no close frame) to simulate a Wi-Fi drop."""

    def __init__(self, url: str, *, seed: int = 0) -> None:
        self.url, self.seed = url, seed
        self.ws: Any = None
        self.ack_seq = 0
        self.credit = 0
        self.last_sent = 0
        self.acks: list[int] = []
        self.received: list[dict[str, Any]] = []
        self.close_code: int | None = None
        self.epoch = 0

    async def connect(self) -> None:
        self.ws = await websockets.connect(f"{self.url}/ws/v1/ingest", max_size=2**20)

    async def hello(
        self, ticket: str, *, resume: bool = False, last_sent_seq: int | None = None, **extra: Any
    ) -> dict[str, Any]:
        hello: dict[str, Any] = {
            "t": "hello",
            "ticket": ticket,
            "proto": 1,
            "codec": "pcm16le",
            "sample_rate": 16000,
            "chunk_ms": 200,
            "resume": resume,
            **extra,
        }
        if resume:
            hello["last_sent_seq"] = last_sent_seq
        await self.ws.send(dumps(hello))
        msg = await self.recv()
        if msg.get("t") == "welcome":
            self.ack_seq, self.credit, self.epoch = msg["ack_seq"], msg["credit"], msg["epoch"]
        return msg

    async def recv(self, wait_s: float = 10.0) -> dict[str, Any]:
        """Next JSON message; ``{"t": "closed", "code": …}`` when the server closed the socket."""
        try:
            raw = await asyncio.wait_for(self.ws.recv(), timeout=wait_s)
        except websockets.ConnectionClosed as exc:
            self.close_code = exc.rcvd.code if exc.rcvd else None
            return {"t": "closed", "code": self.close_code}
        msg = orjson.loads(raw)
        self.received.append(msg)
        if msg["t"] == "ping":
            await self.ws.send(dumps({"t": "pong", "ts": msg["ts"]}))
        elif msg["t"] in ("ack", "credit"):
            self.credit = msg["credit"]
            if msg["t"] == "ack":
                self.ack_seq = max(self.ack_seq, msg["ack_seq"])
                self.acks.append(msg["ack_seq"])
        return msg

    async def send_frame(self, seq: int, **kw: Any) -> None:
        await self.ws.send(frame(seq, self.seed, **kw))
        self.last_sent = max(self.last_sent, seq)

    async def close_hard(self) -> None:
        self.ws.transport.abort()

    async def stream(
        self, *, upto: int, kill_at: int | None = None, end: bool = True, wait_s: float = 30.0
    ) -> dict[str, Any]:
        """Send chunks ``last_sent+1 .. upto`` under the credit rule (plus ``welcome.missing`` and nacks),
        then ``end{final_seq}`` and wait for ``bye``. Returns the last message (``bye`` or ``closed``)."""
        deadline = time.monotonic() + wait_s
        pending: list[int] = []
        for msg in self.received:  # welcome.missing from a resume hello
            if msg.get("t") == "welcome":
                pending = [s for lo, hi in msg.get("missing", []) for s in range(lo, hi + 1)]
        next_seq = self.last_sent + 1
        ended = False
        while time.monotonic() < deadline:
            sent_any = False
            while (
                pending and pending[0] <= self.last_sent
            ):  # ring-buffer re-sends do not count as new outstanding
                await self.ws.send(frame(pending.pop(0), self.seed))
                sent_any = True
            while next_seq <= upto and next_seq - self.ack_seq <= self.credit:
                await self.send_frame(next_seq)
                if kill_at is not None and next_seq == kill_at:
                    await self.close_hard()
                    return {"t": "killed", "at": next_seq}
                next_seq += 1
                sent_any = True
            if next_seq > upto and end and not ended:
                await self.ws.send(dumps({"t": "end", "final_seq": upto}))
                ended = True
            try:
                msg = await self.recv(wait_s=0.05 if sent_any else 1.0)
            except TimeoutError:
                continue
            if msg["t"] == "nack":
                pending += [s for lo, hi in msg["missing"] for s in range(lo, hi + 1) if s not in pending]
            elif msg["t"] in ("bye", "closed", "error"):
                if msg["t"] == "error":
                    return await self.recv()
                return msg
        raise TimeoutError("recorder stream timed out")

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            await self.ws.close()


async def open_viewer(url: str, ticket: str, *, from_seq: int | None = None) -> Any:
    ws = await websockets.connect(f"{url}/ws/v1/watch", max_size=2**20)
    hello: dict[str, Any] = {"t": "hello", "ticket": ticket}
    if from_seq is not None:
        hello["from_seq"] = from_seq
    await ws.send(dumps(hello))
    return ws


async def viewer_recv(ws: Any, wait_s: float = 10.0) -> dict[str, Any]:
    try:
        raw = await asyncio.wait_for(ws.recv(), timeout=wait_s)
    except websockets.ConnectionClosed as exc:
        return {"t": "closed", "code": exc.rcvd.code if exc.rcvd else None}
    msg = orjson.loads(raw)
    if msg["t"] == "ping":
        await ws.send(dumps({"t": "pong", "ts": msg["ts"]}))
    return msg


def utcnow() -> datetime:
    return datetime.now(tz=UTC)


__all__ = [
    "Recorder",
    "Seeded",
    "build_app",
    "frame",
    "ingest_ticket",
    "live_server",
    "open_viewer",
    "payload_for",
    "seed_session",
    "sha_of",
    "utcnow",
    "viewer_recv",
]
_ = field  # dataclass import used above
