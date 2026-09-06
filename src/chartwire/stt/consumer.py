"""One session's chunk-stream consumer inside the stt-worker (spec §7.4).

The consumer owns exactly one ``sess:{sid}:chunks`` stream for as long as the worker holds the
``stt:owner:{sid}`` lease. Everything it does is at-least-once with idempotent effects:

* **dedup** — entries with ``seq <= stt_offsets.last_chunk_seq`` are acknowledged and skipped;
* **gap rebuild** — ``seq > last_chunk_seq + 1`` (stream trimmed, entry lost, Redis flushed) replays
  the missing range from the ``audio_chunks`` ledger + object store (decrypted with the session DEK,
  sha256-verified), waiting 200 ms when a row is not ledgered yet;
* **group vanished** (``NOGROUP`` after ``FLUSHALL``) — the group is recreated at ``$`` and the ledger
  is replayed from ``last_chunk_seq + 1``;
* **finals** — one transaction per final: ``transcript_segments`` (text encrypted, ``created_at =
  started_at + t_start_ms``), the consent-gated ``segment_search`` row, ``risk_events`` for
  alert-worthy hits, the ``stt_offsets`` upsert; then ``XACK``, ``ZADD alerts:sla``, ``PUBLISH``;
* **finals re-emitted after a restart** — an adapter that replays history (the simulator releases
  every past utterance on its first chunk) is de-duplicated by time: a final whose ``t_end_ms`` is
  not past the last persisted segment's is dropped. ``insert_final``'s ``(session_id, seq,
  created_at)`` conflict is the second line of defence.

No transcript text reaches a log line: the logger sees ids, seqs and counters only.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import orjson
from redis.exceptions import ResponseError
from sqlalchemy.ext.asyncio import AsyncSession

from chartwire.audit import service as audit
from chartwire.consent import gates
from chartwire.consent.service import active_scopes_for_patient
from chartwire.core import pii
from chartwire.crypto.envelope import Envelope, aad
from chartwire.crypto.errors import DecryptError, DekDestroyedError
from chartwire.db.repo import search as search_repo
from chartwire.db.repo import segments as segments_repo
from chartwire.db.repo import sessions as sessions_repo
from chartwire.db.tenant import TenantCtx, tenant_tx
from chartwire.ops import metrics
from chartwire.outbox import writer as outbox_writer
from chartwire.redis import keys
from chartwire.risk import alerts
from chartwire.risk.detector import scan
from chartwire.risk.terms import lexicon_tag
from chartwire.stt.base import Chunk, Final, Partial, SessionInfo, SttAdapter, SttStream

log = logging.getLogger(__name__)

TERMINAL_STATES = frozenset({"transcribed", "drafted", "signed", "purging", "purged"})
ENDED_STATES = frozenset({"ended"}) | TERMINAL_STATES
END_WAIT_S = 10.0
"""The recorder shell appends the end marker *before* committing ``state='ended'``; the consumer waits
this long for that commit so ``transcribed`` never overtakes ``ended``."""
REBUILD_WARN_EVERY_S = 5.0


def _group_vanished(exc: ResponseError) -> bool:
    """``NOGROUP`` on a read, or ``UNBLOCKED`` when the key was deleted under a blocking read (FLUSHDB)."""
    text = str(exc)
    return "NOGROUP" in text or "UNBLOCKED" in text


class SessionGone(RuntimeError):
    """The session cannot be served any more (purged, DEK destroyed, row missing)."""


class OwnershipLost(RuntimeError):
    """The owner lease belongs to another worker; stop without touching the stream further."""


@dataclass(frozen=True, slots=True)
class SessionFacts:
    """What the consumer needs from ``sessions`` / ``tenants`` (loaded once by the worker)."""

    tenant_id: UUID
    session_id: UUID
    patient_id: UUID
    script_ref: str | None
    chunk_ms: int
    started_at: datetime
    kek_ref: str
    dek_wrapped: bytes | None
    provider: str
    epoch: int


@dataclass(frozen=True, slots=True)
class ConsumerConfig:
    consumer_name: str
    read_count: int = 32
    block_ms: int = 1000
    autoclaim_idle_ms: int = 60_000
    rebuild_wait_s: float = 0.2
    rebuild_deadline_s: float = 30.0
    """How long ``_rebuild`` waits for one missing ledger row before stepping over it. A chunk whose
    store path was fenced by a newer epoch never reaches the ledger, and without a bound the consumer
    would poll for it forever, holding the owner lease and one ``max_sessions`` slot."""
    fetch_audio: bool = False
    """Read/decrypt audio bytes for every stream entry (real STT); the simulator ignores payloads."""
    consent_cache_s: float = 1.0
    idle_check_s: float = 10.0
    stream_maxlen: int = 2000


@dataclass(slots=True)
class ConsumerStats:
    entries: int = 0
    duplicates: int = 0
    rebuilds: int = 0
    rebuilt_chunks: int = 0
    rebuild_gaps_skipped: int = 0
    group_recreated: int = 0
    finals: int = 0
    duplicate_finals: int = 0
    partials: int = 0
    skipped_no_consent: int = 0
    integrity_failures: int = 0
    alerts: int = 0
    lag: int = 0
    outcome: str | None = None


def chunk_aad(tenant_id: UUID, session_id: UUID, seq: int) -> str:
    """AAD the ingest shell used for ``audio_chunks`` ciphertext (``ws/ingest.py``)."""
    return aad(tenant_id, "session", session_id, f"chunk:{seq}")


def segment_aad(tenant_id: UUID, session_id: UUID, seq: int) -> str:
    """AAD of ``transcript_segments.text_enc`` — the same string ``ws.watch.segment_aad`` builds; every
    reader (viewer replay, notes, REST) decrypts with it."""
    return aad(tenant_id, "session", session_id, f"segment:{seq}")


@dataclass(slots=True)
class _Scopes:
    value: set[str] = field(default_factory=set)
    at: float = -1e9


class SessionConsumer:
    """Consume one session's stream until the end marker (``"transcribed"``), a stop request
    (``"released"``), the loss of the owner lease (``"released"``) or the session going away
    (``"gone"``). ``deps`` is ``HandlerContext``-shaped: ``engine, redis, objectstore, clock, keycache``.
    """

    def __init__(
        self,
        deps: Any,
        adapter: SttAdapter,
        facts: SessionFacts,
        cfg: ConsumerConfig,
        *,
        owns: Callable[[], Awaitable[bool]] | None = None,
    ) -> None:
        self.deps = deps
        self.adapter = adapter
        self.facts = facts
        self.cfg = cfg
        self._owns = owns
        self.stats = ConsumerStats()
        self.stream_key = keys.sess_chunks(facts.session_id)
        self.group = keys.STT_CONSUMER_GROUP
        self.last_chunk_seq = 0
        self.last_segment_seq = -1
        self.last_segment_t_end = -1
        self.dek = b""
        self.stream: SttStream | None = None
        self._scopes = _Scopes()
        self._stop = asyncio.Event()
        self._offsets_dirty = False
        self._offsets_flushed_at = 0.0
        self._last_idle_check = 0.0

    # --- lifecycle ---------------------------------------------------------------------------------

    def stop(self) -> None:
        """Finish the entry in progress, then return ``"released"`` (drain / lease lost)."""
        self._stop.set()

    async def run(self) -> str:
        sid = self.facts.session_id
        try:
            outcome = await self._run()
        except SessionGone as exc:
            log.warning("session cannot be served", extra={"session_id": str(sid), "reason": str(exc)})
            outcome = "gone"
        except OwnershipLost:
            outcome = "released"
        self.stats.outcome = outcome
        log.info(
            "session consumer finished",
            extra={
                "session_id": str(sid),
                "outcome": outcome,
                "entries": self.stats.entries,
                "finals": self.stats.finals,
                "duplicates": self.stats.duplicates,
                "rebuilds": self.stats.rebuilds,
                "last_chunk_seq": self.last_chunk_seq,
            },
        )
        return outcome

    async def _run(self) -> str:
        await self._load()
        self.stream = await self.adapter.open(
            SessionInfo(
                session_id=self.facts.session_id,
                tenant_id=self.facts.tenant_id,
                script_ref=self.facts.script_ref,
                chunk_ms=self.facts.chunk_ms,
                started_at=self.facts.started_at,
            )
        )
        if await self._autoclaim():
            return "transcribed"
        while not self._stop.is_set():
            try:
                batches = await self.deps.redis.xreadgroup(
                    self.group,
                    self.cfg.consumer_name,
                    {self.stream_key: ">"},
                    count=self.cfg.read_count,
                    block=self.cfg.block_ms,
                )
            except ResponseError as exc:
                if not _group_vanished(exc):
                    raise
                if await self._recover_group():
                    await self._on_end(None)  # the end marker died with the flushed stream
                    return "transcribed"
                continue
            entries = batches[0][1] if batches else []
            if not entries:
                if await self._idle_should_exit():
                    await self._flush_offsets(force=True)
                    return "released"
                continue
            if await self._handle_entries(entries):
                return "transcribed"
            await self._update_lag()
        await self._flush_offsets(force=True)
        return "released"

    async def _load(self) -> None:
        f = self.facts
        try:
            self.dek = self.deps.keycache.get(f.session_id, f.kek_ref, f.dek_wrapped)
        except DekDestroyedError as exc:
            raise SessionGone("dek_destroyed") from exc
        async with self._tx() as s:
            offsets = await sessions_repo.get_stt_offset(s, f.session_id)
            if offsets is not None:
                self.last_chunk_seq = int(offsets.last_chunk_seq)
                self.last_segment_seq = int(offsets.last_segment_seq)
            if self.last_segment_seq >= 0:
                rows = await segments_repo.replay(
                    s, f.session_id, self.last_segment_seq - 1, 1, started_at=f.started_at
                )
                if rows:
                    self.last_segment_t_end = int(rows[0].t_end_ms)
        log.info(
            "session consumer start",
            extra={
                "session_id": str(f.session_id),
                "last_chunk_seq": self.last_chunk_seq,
                "last_segment_seq": self.last_segment_seq,
            },
        )

    @contextlib.asynccontextmanager
    async def _tx(self):  # type: ignore[no-untyped-def]
        async with tenant_tx(self.deps.engine, TenantCtx.service(self.facts.tenant_id)) as s:
            yield s

    # --- stream mechanics ----------------------------------------------------------------------------

    async def _autoclaim(self) -> bool:
        """Take over entries a dead/stalled consumer left pending for longer than ``autoclaim_idle_ms``.
        Returns True when the claimed entries included the end marker (session finished)."""
        cursor = "0-0"
        while not self._stop.is_set():
            try:
                result = await self.deps.redis.xautoclaim(
                    self.stream_key,
                    self.group,
                    self.cfg.consumer_name,
                    min_idle_time=self.cfg.autoclaim_idle_ms,
                    start_id=cursor,
                    count=self.cfg.read_count,
                )
            except ResponseError as exc:
                if not _group_vanished(exc):
                    raise
                if await self._recover_group():
                    await self._on_end(None)  # the end marker died with the flushed stream
                    return True
                return False
            cursor, entries = result[0], result[1]
            if entries:
                log.info(
                    "entries autoclaimed",
                    extra={"session_id": str(self.facts.session_id), "count": len(entries)},
                )
                if await self._handle_entries(entries):
                    return True
            if cursor == "0-0":
                return False
        return False

    async def _recover_group(self) -> bool:
        """``XGROUP CREATE … $ MKSTREAM`` then replay the ledger from ``last_chunk_seq + 1`` (§5, §7.4).

        Returns True when ``sessions.state`` is already ``ended``: recreating the group at ``$``
        skipped whatever the flushed stream still held, and the recorder's end marker is written
        *before* the ``ended`` commit, so that marker is one of the things now gone. The ledger
        rebuild replaces the chunks but nothing replaces the marker — so the caller must run the end
        path itself. Re-arming the stream instead would strand the session at ``ended`` forever:
        ``ended`` is not in :data:`TERMINAL_STATES`, ``_idle_should_exit`` only exits when the session
        is *absent* from ``stt:active`` (which this method would have just re-populated), and
        ``session_reaper`` only scans ``recording``/``paused``."""
        self.stats.group_recreated += 1
        with contextlib.suppress(ResponseError):  # BUSYGROUP: the recorder's hello recreated it first
            await self.deps.redis.xgroup_create(self.stream_key, self.group, id="$", mkstream=True)
        log.warning(
            "consumer group recreated; rebuilding from ledger",
            extra={"session_id": str(self.facts.session_id)},
        )
        async with self._tx() as s:
            row = await sessions_repo.get_session(s, self.facts.session_id)
            if row is None or row.state in TERMINAL_STATES:
                raise SessionGone("state" if row is not None else "missing")
            state = str(row.state)
            top = await sessions_repo.max_ledger_seq(s, self.facts.session_id)
        ended = state in ENDED_STATES
        # The flush also emptied stt:active, and the owner lease with it — ``SttWorker.owns`` re-takes a
        # missing lease only for a session that is still listed there, so this SADD is what keeps this
        # consumer the owner through the rebuild. It is safe for an already-``ended`` session too
        # because the caller runs the end path right after, and ``_on_end`` SREMs the member itself.
        await self.deps.redis.sadd(keys.STT_ACTIVE, str(self.facts.session_id))
        if top > self.last_chunk_seq:
            await self._rebuild(self.last_chunk_seq + 1, top)
        return ended

    async def _idle_should_exit(self) -> bool:
        """Every ``idle_check_s`` with an empty stream: stop serving a session nobody lists as active
        (purged, or ended without a marker) — the worker releases the lease and moves on."""
        mono = self.deps.clock.monotonic()
        if mono - self._last_idle_check < self.cfg.idle_check_s:
            return False
        self._last_idle_check = mono
        await self._flush_offsets(force=False)
        active = await self.deps.redis.sismember(keys.STT_ACTIVE, str(self.facts.session_id))
        return not active

    async def _handle_entries(self, entries: list[tuple[str, Mapping[str, str]]]) -> bool:
        """Process delivered entries in order; True once the end marker has been handled."""
        for entry_id, fields in entries:
            if self._stop.is_set():
                return False
            self.stats.entries += 1
            if fields.get("end") == "1":
                await self._on_end(entry_id)
                return True
            seq = int(fields["seq"])
            if seq <= self.last_chunk_seq:
                self.stats.duplicates += 1
                await self._xack(entry_id)
                continue
            if seq > self.last_chunk_seq + 1:
                await self._rebuild(self.last_chunk_seq + 1, seq - 1)
                if seq > self.last_chunk_seq + 1:
                    # _rebuild was aborted by stop() (drain / lease lost) with the gap still open.
                    # Advancing now would push last_chunk_seq — and, via the GREATEST upsert in
                    # ``upsert_stt_offset``, the persisted offset — past chunks nobody transcribed,
                    # and no later owner would ever go back for them. Leave the entry unacked so
                    # XAUTOCLAIM re-delivers it to the next owner instead.
                    log.warning(
                        "gap still open after rebuild; leaving entry for the next owner",
                        extra={
                            "session_id": str(self.facts.session_id),
                            "seq": seq,
                            "last_chunk_seq": self.last_chunk_seq,
                        },
                    )
                    return False
            payload = b""
            if self.cfg.fetch_audio:
                payload = await self._fetch_payload(seq, fields["key"], expected_sha=None)
            chunk = Chunk(
                session_id=self.facts.session_id,
                seq=seq,
                offset_ms=int(fields["off"]),
                flags=int(fields.get("fl") or 0),
                payload=payload,
            )
            await self._process(chunk, entry_id=entry_id, received_ms=int(fields.get("ts") or 0))
        return False

    async def _xack(self, entry_id: str | None) -> None:
        if entry_id is not None:
            await self.deps.redis.xack(self.stream_key, self.group, entry_id)

    async def _update_lag(self) -> int:
        """``stt:lag`` = entries delivered-but-unacked + not yet delivered (Redis 7 ``XINFO GROUPS``)."""
        lag = 0
        with contextlib.suppress(ResponseError):
            for group in await self.deps.redis.xinfo_groups(self.stream_key):
                if group.get("name") == self.group:
                    lag = int(group.get("pending") or 0) + int(group.get("lag") or 0)
        self.stats.lag = lag
        await self.deps.redis.hset(keys.STT_LAG, str(self.facts.session_id), str(lag))
        return lag

    # --- rebuild from the ledger ---------------------------------------------------------------------

    async def _fetch_payload(self, seq: int, storage_key: str, *, expected_sha: bytes | None) -> bytes:
        f = self.facts
        blob = await self.deps.objectstore.get(storage_key)
        try:
            payload = Envelope.decrypt(self.dek, blob, chunk_aad(f.tenant_id, f.session_id, seq))
        except DecryptError as exc:
            self.stats.integrity_failures += 1
            log.error(
                "chunk decrypt failed",
                extra={"session_id": str(f.session_id), "seq": seq, "reason": str(exc)},
            )
            return b""
        if expected_sha is not None and hashlib.sha256(payload).digest() != bytes(expected_sha):
            self.stats.integrity_failures += 1
            log.error("chunk sha256 mismatch", extra={"session_id": str(f.session_id), "seq": seq})
        return payload

    async def _rebuild(self, from_seq: int, to_seq: int) -> None:
        """Replay ``audio_chunks`` rows ``from_seq..to_seq`` through the adapter; a hole means the
        ingest has not ledgered the row yet — wait ``rebuild_wait_s`` and look again (§7.4).

        Two bounds keep this from becoming a parked loop. The wait on any single seq is capped at
        ``rebuild_deadline_s``: a chunk whose store path was fenced by a newer epoch never reaches the
        ledger at all, and if the recorder never comes back to re-send it the hole is permanent — past
        the deadline it is logged, counted in ``rebuild_gaps_skipped`` and stepped over so the read
        loop resumes. And each pass re-checks the owner lease, so a stalled ex-owner raises
        :class:`OwnershipLost` here instead of transcribing behind the new owner's back.

        Aborts early when :meth:`stop` is set (drain / lease lost) — possibly with the gap still open,
        which is why callers must re-check ``last_chunk_seq`` before advancing past it."""
        f = self.facts
        self.stats.rebuilds += 1
        metrics.STT_REBUILDS_TOTAL.inc()  # scenario D ``rebuild_count`` (§11.2)
        log.warning(
            "rebuild", extra={"session_id": str(f.session_id), "from_seq": from_seq, "to_seq": to_seq}
        )
        expected = from_seq
        waited = 0.0
        deadline = self.deps.clock.monotonic() + self.cfg.rebuild_deadline_s
        while expected <= to_seq and not self._stop.is_set():
            async with self._tx() as s:
                rows = await sessions_repo.chunks_between(s, f.session_id, expected, to_seq)
            before = expected
            for row in rows:
                if int(row.seq) != expected:
                    break
                payload = await self._fetch_payload(int(row.seq), row.storage_key, expected_sha=row.sha256)
                chunk = Chunk(f.session_id, int(row.seq), int(row.offset_ms), int(row.flags), payload)
                received = row.received_at
                await self._process(chunk, entry_id=None, received_ms=int(received.timestamp() * 1000))
                self.stats.rebuilt_chunks += 1
                expected += 1
            if expected > to_seq:
                break
            if expected > before:  # progress: the next hole gets a fresh budget
                deadline = self.deps.clock.monotonic() + self.cfg.rebuild_deadline_s
                waited = 0.0
            await self._check_owner()
            if self.deps.clock.monotonic() >= deadline:
                self.stats.rebuild_gaps_skipped += 1
                log.error(
                    "rebuild gap abandoned; chunk never reached the ledger",
                    extra={"session_id": str(f.session_id), "seq": expected, "to_seq": to_seq},
                )
                self.last_chunk_seq = max(self.last_chunk_seq, expected)
                self._offsets_dirty = True
                expected += 1
                deadline = self.deps.clock.monotonic() + self.cfg.rebuild_deadline_s
                waited = 0.0
                continue
            await asyncio.sleep(self.cfg.rebuild_wait_s)
            waited += self.cfg.rebuild_wait_s
            if waited >= REBUILD_WARN_EVERY_S:
                waited = 0.0
                log.warning(
                    "rebuild waiting for ledger rows",
                    extra={"session_id": str(f.session_id), "seq": expected, "to_seq": to_seq},
                )

    # --- per chunk ---------------------------------------------------------------------------------

    async def _scopes_cached(self) -> set[str]:
        mono = self.deps.clock.monotonic()
        if mono - self._scopes.at >= self.cfg.consent_cache_s:
            async with self._tx() as s:
                self._scopes.value = await active_scopes_for_patient(s, self.facts.patient_id)
            self._scopes.at = mono
        return self._scopes.value

    async def _process(self, chunk: Chunk, *, entry_id: str | None, received_ms: int) -> None:
        assert self.stream is not None
        scopes = await self._scopes_cached()
        if not gates.has_scope(scopes, "transcription"):
            # §8.3: no adapter call, but the offset still advances so the stream drains.
            self.stats.skipped_no_consent += 1
            self.last_chunk_seq = chunk.seq
            self._offsets_dirty = True
            await self._xack(entry_id)
            await self._flush_offsets(force=False)
            return
        events = await self.stream.feed(chunk)
        self.last_chunk_seq = chunk.seq
        self._offsets_dirty = True
        for event in events:
            if isinstance(event, Partial):
                await self._publish_partial(event)
            elif isinstance(event, Final):
                await self._commit_final(event, chunk_seq=chunk.seq, received_ms=received_ms)
        await self._xack(entry_id)
        await self._flush_offsets(force=False)

    async def _publish_partial(self, partial: Partial) -> None:
        self.stats.partials += 1
        msg = {"t": "transcript.partial", "from_seq": partial.from_seq, "text": partial.text}
        await self.deps.redis.publish(keys.sess_events(self.facts.session_id), orjson.dumps(msg).decode())

    async def _flush_offsets(self, *, force: bool) -> None:
        """Persist ``stt_offsets`` for progress that produced no final (throttled to once per second)."""
        if not self._offsets_dirty:
            return
        mono = self.deps.clock.monotonic()
        if not force and mono - self._offsets_flushed_at < 1.0:
            return
        await self._check_owner()  # never let a stalled ex-owner regress another worker's offsets
        async with self._tx() as s:
            await self._upsert_offsets(s)
        self._offsets_dirty = False
        self._offsets_flushed_at = mono

    async def _upsert_offsets(self, s: AsyncSession) -> None:
        await sessions_repo.upsert_stt_offset(
            s,
            session_id=self.facts.session_id,
            tenant_id=self.facts.tenant_id,
            last_chunk_seq=self.last_chunk_seq,
            last_segment_seq=self.last_segment_seq,
        )

    async def _check_owner(self) -> None:
        if self._owns is not None and not await self._owns():
            raise OwnershipLost(str(self.facts.session_id))

    # --- the final-segment transaction (§7.4) ---------------------------------------------------

    async def _commit_final(self, final: Final, *, chunk_seq: int, received_ms: int) -> int | None:
        f = self.facts
        if final.t_end_ms <= self.last_segment_t_end:
            self.stats.duplicate_finals += 1
            return None
        await self._check_owner()
        seg_seq = self.last_segment_seq + 1
        created_at = f.started_at + timedelta(milliseconds=final.t_start_ms)
        text_enc = Envelope.encrypt(
            self.dek, final.text.encode("utf-8"), segment_aad(f.tenant_id, f.session_id, seg_seq)
        )
        now = self.deps.clock.now()
        events = []
        async with self._tx() as s:
            scopes = await active_scopes_for_patient(s, f.patient_id)
            row = await segments_repo.insert_final(
                s,
                tenant_id=f.tenant_id,
                session_id=f.session_id,
                patient_id=f.patient_id,
                seq=seg_seq,
                speaker=final.speaker,
                t_start_ms=final.t_start_ms,
                t_end_ms=final.t_end_ms,
                text_enc=text_enc,
                text_len=len(final.text),
                confidence=final.confidence,
                provider=f.provider,
                created_at=created_at,
            )
            if gates.has_scope(scopes, "search_index"):
                await search_repo.index_segment(
                    s,
                    segment_id=row.id,
                    segment_created_at=row.created_at,
                    tenant_id=f.tenant_id,
                    session_id=f.session_id,
                    patient_id=f.patient_id,
                    speaker=final.speaker,
                    text_plain=pii.redact(final.text),  # spec §10.2 — 색인은 가리고 text_enc 는 원문을 지킨다
                    terms=lexicon_tag(final.text),
                )
            for hit in scan(final.text, final.speaker):
                metrics.RISK_HITS_TOTAL.labels(
                    hit.category, str(hit.severity), str(hit.suppressed).lower()
                ).inc()
                if hit.alerts:
                    events.append(
                        await alerts.create_event(
                            s,
                            tenant_id=f.tenant_id,
                            session_id=f.session_id,
                            patient_id=f.patient_id,
                            segment_id=row.id,
                            segment_created_at=row.created_at,
                            segment_seq=seg_seq,
                            hit=hit,
                            now=now,
                        )
                    )
            self.last_chunk_seq = max(self.last_chunk_seq, chunk_seq)
            self.last_segment_seq = seg_seq
            await self._upsert_offsets(s)
        committed_at = self.deps.clock.now()
        self.last_segment_t_end = final.t_end_ms
        self._offsets_dirty = False
        self._offsets_flushed_at = self.deps.clock.monotonic()
        self.stats.finals += 1
        self._scopes = _Scopes(scopes, self.deps.clock.monotonic())
        if received_ms:
            metrics.SEGMENT_E2E_SECONDS.observe(max(0.0, committed_at.timestamp() - received_ms / 1000.0))
        msg = {
            "t": "transcript.final",
            "seq": seg_seq,
            "speaker": final.speaker,
            "t_start_ms": final.t_start_ms,
            "t_end_ms": final.t_end_ms,
            "text": final.text,
            "confidence": final.confidence,
            "segment_id": int(row.id),
            "committed_at": committed_at.isoformat(),
        }
        await self.deps.redis.publish(keys.sess_events(f.session_id), orjson.dumps(msg).decode())
        for event in events:
            self.stats.alerts += 1
            await alerts.after_commit(self.deps.redis, event, committed_at=committed_at)
        return int(row.id)

    # --- end marker (§7.4) ---------------------------------------------------------------------------

    async def _on_end(self, entry_id: str | None) -> None:
        """Flush the adapter, set ``transcribed``, emit the outbox event and release ``stt:active``.
        ``entry_id`` is None when the end marker itself was lost (Redis flush after ``ended``)."""
        assert self.stream is not None
        f = self.facts
        self._scopes = _Scopes()  # re-read: the flush is an adapter call and needs the same gate (§8.3)
        if gates.has_scope(await self._scopes_cached(), "transcription"):
            for event in await self.stream.flush():
                if isinstance(event, Final):
                    await self._commit_final(event, chunk_seq=self.last_chunk_seq, received_ms=0)
        await self._check_owner()
        now = self.deps.clock.now()
        await self._wait_for_ended()
        async with self._tx() as s:
            row = await sessions_repo.get_session(s, f.session_id)
            if row is None:
                raise SessionGone("missing")
            if row.state not in TERMINAL_STATES:
                await sessions_repo.set_state(s, f.session_id, "transcribed", now=now)
            event_id = await outbox_writer.emit(
                s,
                tenant_id=f.tenant_id,
                aggregate_type="session",
                aggregate_id=f.session_id,
                event_type="session.transcribed",
                payload={"session_id": str(f.session_id), "patient_id": str(f.patient_id)},
                idempotency_key=outbox_writer.idempotency_key("session.transcribed", f.session_id, row.epoch),
            )
            if event_id is not None:
                await audit.record(
                    s,
                    tenant_id=f.tenant_id,
                    actor_id=None,
                    actor_role="service",
                    action="session.transcribed",
                    resource_type="session",
                    resource_id=f.session_id,
                    detail={"segments": self.last_segment_seq + 1, "last_chunk_seq": self.last_chunk_seq},
                )
            await self._upsert_offsets(s)
        self._offsets_dirty = False
        await self._xack(entry_id)
        redis = self.deps.redis
        await redis.publish(
            keys.sess_events(f.session_id),
            orjson.dumps({"t": "session.state", "state": "transcribed"}).decode(),
        )
        await redis.publish(keys.OUTBOX_WAKE, "1")
        await redis.srem(keys.STT_ACTIVE, str(f.session_id))
        await redis.hdel(keys.STT_LAG, str(f.session_id))
        log.info(
            "session transcribed",
            extra={
                "session_id": str(f.session_id),
                "segments": self.last_segment_seq + 1,
                "outbox_event": event_id,
            },
        )

    async def _wait_for_ended(self) -> None:
        deadline = self.deps.clock.monotonic() + END_WAIT_S
        while True:
            async with self._tx() as s:
                row = await sessions_repo.get_session(s, self.facts.session_id)
            if row is None or row.state in ENDED_STATES:
                return
            if self.deps.clock.monotonic() >= deadline:
                log.warning(
                    "end marker before state=ended; proceeding",
                    extra={"session_id": str(self.facts.session_id)},
                )
                return
            await asyncio.sleep(0.2)


def utcnow() -> datetime:
    return datetime.now(tz=UTC)
