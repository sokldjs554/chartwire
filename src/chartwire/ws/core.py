"""Sans-I/O protocol cores (spec §6.5): ``IngestCore`` (recorder) and ``WatchCore`` (viewer).

No I/O here: each method takes one event (time arrives as ``now_ms``) and returns the
:mod:`chartwire.ws.actions` the async shell executes in order."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from chartwire.ws import actions as act
from chartwire.ws import credit as credit_rules
from chartwire.ws import messages as m
from chartwire.ws.actions import Action, CloseCode, Range

ACK_EVERY_CHUNKS = 8
ACK_EVERY_MS = 100
END_WAIT_MS = 10_000
DRAIN_WAIT_MS = 20_000
WATCH_PENDING_MAX = 1_024


class IngestCore:
    """Recorder state machine; invariants: ``ack_seq ≤ ledger_seq``, contiguous prefixes, outstanding ≤ credit+20."""

    def __init__(self, *, now_ms: int, credit_base: int, reorder_max: int = 64, session_id: str = "") -> None:
        self.sid, self.credit_base, self.reorder_max = session_id, credit_base, reorder_max
        self.now_ms, self.epoch, self.closed = now_ms, 0, False
        self.ack_seq = self.ledger_seq = self.contig_seq = self.highest_seen = 0
        self.credit = self.advertised = credit_base
        self.final_seq: int | None = None
        self.stats = act.IngestStats()
        self._reorder: dict[int, tuple[int, int]] = {}  # seq → (offset_ms, flags), seq > contig_seq + 1
        self._ledgered: set[int] = set()  # committed but not yet contiguous with ledger_seq
        self._last_ack_ms = self._last_nack_ms = now_ms
        self._last_nack: list[Range] = []
        self._zero_since: int | None = None
        self._paused = False
        self._end_deadline: int | None = None
        self._drain_deadline: int | None = None
        self._hb = credit_rules.Heartbeat(now_ms)

    @property
    def outstanding(self) -> int:
        return self.highest_seen - self.ack_seq

    @property
    def missing(self) -> list[Range]:
        top = max(self.highest_seen, self.final_seq or 0)
        pending = range(self.ledger_seq + 1, top + 1)
        return act.ranges(
            s for s in pending if s > self.contig_seq and s not in self._reorder and s not in self._ledgered
        )

    def on_hello(
        self,
        *,
        ack_seq_from_store: int,
        resume: bool,
        last_sent_seq: int | None,
        epoch: int,
        now_ms: int,
        ledgered_after_ack: Iterable[int] = (),
        hot_state_present: bool = True,
    ) -> list[Action]:
        self.now_ms, self.epoch = now_ms, epoch
        self.ack_seq = self.ledger_seq = self.contig_seq = self.highest_seen = ack_seq_from_store
        self._ledgered = {s for s in ledgered_after_ack if s > ack_seq_from_store}
        self._advance_ledger()
        out: list[Action] = [] if hot_state_present else [act.Rehydrate()]
        if resume and last_sent_seq is not None and last_sent_seq > self.ack_seq:
            if last_sent_seq - self.ack_seq > credit_rules.RING_BUFFER_CHUNKS:
                return self._fail(CloseCode.SEQ_GAP_UNRECOVERABLE, "resume gap exceeds ring buffer")
            self.highest_seen = last_sent_seq
        self.advertised = self.credit  # one core per connection: welcome advertises the current credit
        welcome = m.Welcome(
            session_id=self.sid, epoch=epoch, ack_seq=self.ack_seq, credit=self.credit, missing=self.missing
        )
        return [*out, act.Transition("recording"), act.Send(m.dump(welcome))]

    def on_chunk(self, seq: int, offset_ms: int, flags: int, now_ms: int) -> list[Action]:
        if self.closed:
            return []
        self.now_ms = now_ms
        self.stats.received += 1
        if self.final_seq is not None and seq > self.final_seq:
            return self._fail(CloseCode.SESSION_ENDED, "chunk after final_seq")
        if seq <= self.contig_seq or seq in self._reorder:
            self.stats.duplicates += 1
            return [act.Ack(self.ack_seq, self._advertise())] if seq <= self.ack_seq else []
        if seq > self.highest_seen and credit_rules.violates(seq - self.ack_seq, self.advertised):
            return self._fail(CloseCode.CREDIT_VIOLATION, "outstanding exceeds credit + tolerance")
        self.highest_seen = max(self.highest_seen, seq)
        if seq == self.contig_seq + 1:
            return self._store(seq, offset_ms, flags)
        self.stats.reordered += 1
        if len(self._reorder) < self.reorder_max:
            self._reorder[seq] = (offset_ms, flags)
        else:
            self.stats.dropped += 1  # the nack below makes the recorder re-send it later
        return self._nack()

    def on_stored(self, seq: int) -> list[Action]:
        self.stats.stored += 1
        return []

    def on_ledgered(self, seqs: Iterable[int]) -> list[Action]:
        self._ledgered.update(s for s in seqs if s > self.ledger_seq)
        self._advance_ledger()
        return self._maybe_ack() + self._maybe_finish()

    def on_end(self, final_seq: int, now_ms: int) -> list[Action]:
        if self.closed:
            return []
        self.now_ms = now_ms
        if final_seq < self.highest_seen or (self.final_seq is not None and final_seq != self.final_seq):
            return self._fail(CloseCode.BAD_FRAME, "final_seq below received seqs")
        self.final_seq, self._end_deadline, self.highest_seen = final_seq, now_ms + END_WAIT_MS, final_seq
        return self._maybe_finish() or self._nack()

    def on_tick(self, now_ms: int, stt_lag: int, node_pending: int) -> list[Action]:
        if self.closed:
            return []
        self.now_ms = now_ms
        self.credit = credit_rules.compute(self.credit_base, stt_lag, node_pending)
        out: list[Action] = []
        if credit_rules.changed_significantly(self.advertised, self.credit):
            out.append(act.SendCredit(self._advertise()))
        out += self._pause_check(now_ms) + self._maybe_ack() + self._maybe_finish()
        if self._end_deadline is not None and now_ms >= self._end_deadline and not self.closed:
            self._end_deadline = now_ms + END_WAIT_MS
            out += self._nack(force=True)
        return out + self._hb.tick(now_ms) if not self.closed else out

    def on_pong(self, now_ms: int) -> list[Action]:
        self._hb.pong()
        return []

    def on_superseded(self, new_epoch: int) -> list[Action]:
        if self.closed or new_epoch <= self.epoch:
            return []
        return self._finish(CloseCode.SUPERSEDED, "superseded")

    def on_consent_revoked(self) -> list[Action]:
        return [] if self.closed else self._finish(CloseCode.CONSENT_MISSING, "consent_revoked")

    def on_drain(self, now_ms: int) -> list[Action]:
        if self.closed or self._drain_deadline is not None:
            return []
        self.now_ms, self._drain_deadline = now_ms, now_ms + DRAIN_WAIT_MS
        return [act.Send(m.dump(m.Bye(reason="drain", ack_seq=self.ack_seq))), *self._maybe_finish()]

    def _advertise(self) -> int:
        self.advertised = self.credit
        return self.advertised

    def _store(self, seq: int, offset_ms: int, flags: int) -> list[Action]:
        out: list[Action] = [act.Store(seq, offset_ms, flags)]
        self.contig_seq = seq
        while (nxt := self.contig_seq + 1) in self._reorder:
            off, fl = self._reorder.pop(nxt)
            out.append(act.Store(nxt, off, fl))
            self.contig_seq = nxt
        return out

    def _advance_ledger(self) -> None:
        while (nxt := self.ledger_seq + 1) in self._ledgered:
            self._ledgered.discard(nxt)
            self.ledger_seq = nxt

    def _maybe_ack(self, *, force: bool = False) -> list[Action]:
        if self.ledger_seq <= self.ack_seq or self.closed:
            return []
        due = self.ledger_seq - self.ack_seq >= ACK_EVERY_CHUNKS or self.credit < credit_rules.LOW_WATER
        if not (force or due or self.now_ms - self._last_ack_ms >= ACK_EVERY_MS):
            return []
        self.ack_seq, self._last_ack_ms = self.ledger_seq, self.now_ms
        self.stats.acks += 1
        return [act.Ack(self.ack_seq, self._advertise())]

    def _nack(self, *, force: bool = False) -> list[Action]:
        missing = self.missing  # re-nack only for newly missing seqs, or every 100 ms
        covered = all(any(a <= lo and hi <= b for a, b in self._last_nack) for lo, hi in missing)
        if not missing or (covered and self.now_ms - self._last_nack_ms < ACK_EVERY_MS and not force):
            return []
        self._last_nack, self._last_nack_ms = missing, self.now_ms
        self.stats.nacks += 1
        return [act.Nack(tuple(missing))]

    def _pause_check(self, now_ms: int) -> list[Action]:
        if self.credit > 0:
            self._zero_since, self._paused = None, False
            return []
        self._zero_since = now_ms if self._zero_since is None else self._zero_since
        if self._paused or now_ms - self._zero_since <= credit_rules.ZERO_PAUSE_MS:
            return []
        self._paused = True
        return [act.Send(m.dump(m.PauseNotice(reason="stt_lag", retry_ms=credit_rules.ZERO_PAUSE_MS)))]

    def _maybe_finish(self) -> list[Action]:
        if self.closed:
            return []
        if self.final_seq is not None and self.ledger_seq >= self.final_seq:
            return [act.Transition("ended"), *self._finish(CloseCode.ENDED, "ended")]
        drain = self._drain_deadline
        if drain is not None and (self.ledger_seq >= self.contig_seq or self.now_ms >= drain):
            return self._finish(CloseCode.SERVICE_RESTART, "drain")
        return []

    def _finish(self, code: CloseCode, reason: m.ByeReason) -> list[Action]:
        out = self._maybe_ack(force=True)
        self.closed = True
        return [*out, act.Send(m.dump(m.Bye(reason=reason, ack_seq=self.ack_seq))), act.close(code, reason)]

    def _fail(self, code: CloseCode, message: str) -> list[Action]:
        out: list[Action] = [act.Send(act.error_msg(code, message))]
        if code is CloseCode.SEQ_GAP_UNRECOVERABLE:
            out.append(act.Transition("ended"))
        self.closed = True
        return [*out, act.close(code)]


class WatchCore:
    """Viewer state machine: ``Subscribe`` before ``Replay``; finals strictly increasing, no gaps, no dups."""

    def __init__(self, *, now_ms: int, session_id: str = "") -> None:
        self.sid, self.closed = session_id, False
        self.last_final_seq, self.replaying = -1, False
        self._pending: dict[int, dict[str, Any]] = {}  # live finals held back until contiguous
        self._alerts: set[str] = set()
        self._hb = credit_rules.Heartbeat(now_ms)

    def on_hello(
        self, from_seq: int | None, *, state: m.SessionState = "recording", last_final_seq: int = -1
    ) -> list[Action]:
        self.last_final_seq, self.replaying = (from_seq or 0) - 1, True
        welcome = m.WatchWelcome(session_id=self.sid, state=state, last_final_seq=last_final_seq)
        return [act.Subscribe(), act.Send(m.dump(welcome)), act.Replay(self.last_final_seq)]

    def on_replay_batch(self, finals: list[dict[str, Any]]) -> list[Action]:
        out: list[Action] = []
        for ev in finals:
            if ev.get("t", "transcript.final") != "transcript.final":
                out += self._alert(ev)
            elif int(ev["seq"]) > self.last_final_seq:
                self.last_final_seq = int(ev["seq"])
                out.append(act.Send(ev))
        return out

    def on_replay_done(self) -> list[Action]:
        self.replaying = False
        out = self._flush()
        if self._pending:  # a live final beyond the replayed prefix → fill the gap from the DB
            self.replaying = True
            out.append(act.Replay(self.last_final_seq))
        return out

    def on_live_event(self, ev: dict[str, Any]) -> list[Action]:
        if self.closed:
            return []
        kind = ev.get("t")
        if kind == "risk.alert":
            return self._alert(ev)
        if kind != "transcript.final":
            return [act.Send(ev)]
        seq = int(ev["seq"])
        if seq <= self.last_final_seq or seq in self._pending:
            return []
        self._pending[seq] = ev
        if len(self._pending) > WATCH_PENDING_MAX:
            self.closed = True
            return [act.close(CloseCode.VIEWER_TOO_SLOW)]
        return [] if self.replaying else self.on_replay_done()

    def on_tick(self, now_ms: int) -> list[Action]:
        out: list[Action] = [] if self.closed else self._hb.tick(now_ms)
        self.closed |= any(isinstance(a, act.Close) for a in out)
        return out

    def on_pong(self, now_ms: int) -> list[Action]:
        self._hb.pong()
        return []

    def _alert(self, ev: dict[str, Any]) -> list[Action]:
        rid = str(ev.get("risk_event_id"))
        if rid in self._alerts:
            return []
        self._alerts.add(rid)
        return [act.Send(ev)]

    def _flush(self) -> list[Action]:
        out: list[Action] = []
        self._pending = {s: e for s, e in self._pending.items() if s > self.last_final_seq}
        while (nxt := self.last_final_seq + 1) in self._pending:
            out.append(act.Send(self._pending.pop(nxt)))
            self.last_final_seq = nxt
        return out
