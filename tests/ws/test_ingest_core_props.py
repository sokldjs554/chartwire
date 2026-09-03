"""Hypothesis stateful test for ``IngestCore`` (spec §6.5).

The machine drives a credit-compliant recorder over a lossy, reordering network with duplicate
delivery, out-of-order ledger commits, arbitrary ticks, superseding and disconnect+resume.
Checked after every step:

* no ``Ack`` for a seq that is not durably ledgered (1..ack_seq ⊆ the model's committed set)
* ``ack_seq`` is monotone and never ahead of the core's contiguous ``ledger_seq``
* a compliant recorder is never closed with 4009 (credit), 4000 (heartbeat) or 4008 (resume gap)
* a seq is ``Store``d at most once per connection and only if the recorder actually sent it

At teardown the recorder sends ``end`` and the model delivers everything: the session must close
with 1000, the final ack must equal ``final_seq`` and the set of stored seqs must equal the set
of sent seqs (loss 0; duplicates exist only as re-sends).

Modelling assumption (spec §6.4 rule 3): credit is recomputed from STT lag every 200 ms and
moves by a few chunks per second, so the advertised credit never collapses by more than the
+20 tolerance while a frame is in flight. The ``tick`` rule clamps the credit inputs to that.
"""

from __future__ import annotations

from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, initialize, invariant, precondition, rule

from chartwire.ws import credit
from chartwire.ws.actions import Ack, Action, Close, CloseCode, Nack, Send, SendCredit, Store, Transition
from chartwire.ws.core import END_WAIT_MS, IngestCore

BASE = 30
FORBIDDEN_CLOSES = {CloseCode.CREDIT_VIOLATION, CloseCode.HEARTBEAT_TIMEOUT, CloseCode.SEQ_GAP_UNRECOVERABLE}
Frame = tuple[int, int, int, int | None]  # seq, offset_ms, flags, credit the recorder sent it under


class Recorder:
    """Model of a spec-compliant recorder: ring buffer, credit rule, nack/resume re-sends."""

    def __init__(self) -> None:
        self.last_sent = 0
        self.ack = 0
        self.credit = BASE
        self.sent: dict[int, tuple[int, int]] = {}  # every chunk ever produced (seq → offset, flags)

    def can_send(self) -> bool:
        return self.last_sent - self.ack < self.credit

    def next_chunk(self) -> Frame:
        self.last_sent += 1
        self.sent[self.last_sent] = ((self.last_sent - 1) * 200, 0)
        return (self.last_sent, *self.sent[self.last_sent], self.credit)

    def resend(self, ranges) -> list[Frame]:
        out: list[Frame] = []
        for lo, hi in ranges:
            for seq in range(lo, hi + 1):
                assert seq > self.last_sent - credit.RING_BUFFER_CHUNKS, "nack outside the ring buffer"
                assert seq in self.sent, "server asked for a seq the recorder never sent"
                out.append((seq, *self.sent[seq], None))
        return out


class IngestMachine(RuleBasedStateMachine):
    @initialize()
    def start(self) -> None:
        self.now = 0
        self.rec = Recorder()
        self.ledgered: set[int] = set()  # rows committed in PostgreSQL (survive reconnects)
        self.pending: list[int] = []  # stored on this connection, not yet committed
        self.network: list[Frame] = []  # recorder → server frames in flight
        self.stored_this_conn: set[int] = set()
        self.stored_ever: set[int] = set()
        self.acks: list[int] = []
        self.closed: Close | None = None
        self.epoch = 0
        self.connect(resume=False)

    # --- the model of the async shell ----------------------------------------------------------

    def connect(self, *, resume: bool) -> None:
        self.epoch += 1
        self.core = IngestCore(now_ms=self.now, credit_base=BASE, session_id="sess")
        self.stored_this_conn, self.pending, self.network, self.closed = set(), [], [], None
        contiguous = 0  # sessions.ack_seq as written by the ledger batcher: the durable contiguous prefix
        while contiguous + 1 in self.ledgered:
            contiguous += 1
        self.apply(
            self.core.on_hello(
                ack_seq_from_store=contiguous,
                resume=resume,
                last_sent_seq=self.rec.last_sent if resume else None,
                epoch=self.epoch,
                now_ms=self.now,
                ledgered_after_ack=[s for s in self.ledgered if s > contiguous],
            )
        )

    def deliver(self, frame: Frame) -> None:
        seq, off, fl, _ = frame
        self.apply(self.core.on_chunk(seq, off, fl, self.now))

    def apply(self, actions: list[Action]) -> None:
        for a in actions:
            if isinstance(a, Store):
                assert a.seq in self.rec.sent and self.rec.sent[a.seq] == (a.offset_ms, a.flags)
                assert a.seq not in self.stored_this_conn, "stored twice on one connection"
                self.stored_this_conn.add(a.seq)
                self.stored_ever.add(a.seq)
                self.pending.append(a.seq)
                self.core.on_stored(a.seq)
            elif isinstance(a, Ack):
                assert all(s in self.ledgered for s in range(1, a.ack_seq + 1)), "ack for a non-ledgered seq"
                assert not self.acks or a.ack_seq >= self.acks[-1], "ack not monotone"
                assert a.ack_seq <= self.core.ledger_seq
                self.acks.append(a.ack_seq)
                self.rec.ack, self.rec.credit = max(self.rec.ack, a.ack_seq), a.credit
            elif isinstance(a, Nack):
                self.network.extend(self.rec.resend(a.missing))
            elif isinstance(a, SendCredit):
                self.rec.credit = a.credit
            elif isinstance(a, Send):
                if a.msg["t"] == "welcome":
                    self.rec.ack, self.rec.credit = max(self.rec.ack, a.msg["ack_seq"]), a.msg["credit"]
                    self.network.extend(self.rec.resend(a.msg["missing"]))
                elif a.msg["t"] == "ping":
                    self.apply(self.core.on_pong(self.now))
            elif isinstance(a, Close):
                assert CloseCode(a.code) not in FORBIDDEN_CLOSES, a
                self.closed = a
            else:
                assert isinstance(a, Transition)

    # --- rules ---------------------------------------------------------------------------------

    @precondition(lambda self: self.closed is None)
    @rule(n=st.integers(1, 12))
    def send(self, n: int) -> None:
        for _ in range(n):
            if not self.rec.can_send():
                break
            self.network.append(self.rec.next_chunk())

    @precondition(lambda self: self.network and self.closed is None)
    @rule()
    def deliver_in_order(self) -> None:
        self.deliver(self.network.pop(0))

    @precondition(lambda self: len(self.network) > 1 and self.closed is None)
    @rule(data=st.data())
    def deliver_reordered(self, data: st.DataObject) -> None:
        self.deliver(self.network.pop(data.draw(st.integers(1, len(self.network) - 1))))

    @precondition(lambda self: self.rec.sent and self.closed is None)
    @rule(data=st.data())
    def duplicate(self, data: st.DataObject) -> None:
        seq = data.draw(st.sampled_from(sorted(self.rec.sent)))
        self.deliver((seq, *self.rec.sent[seq], None))

    @precondition(lambda self: self.network)
    @rule(data=st.data())
    def drop_in_flight(self, data: st.DataObject) -> None:
        self.network.pop(data.draw(st.integers(0, len(self.network) - 1)))

    @precondition(lambda self: self.pending and self.closed is None)
    @rule(data=st.data())
    def commit_ledger(self, data: st.DataObject) -> None:
        k = data.draw(st.integers(1, len(self.pending)))
        batch = data.draw(st.permutations(self.pending))[:k]
        self.pending = [s for s in self.pending if s not in batch]
        self.ledgered.update(batch)
        self.apply(self.core.on_ledgered(batch))

    @precondition(lambda self: self.closed is None)
    @rule(dt=st.integers(1, 400), lag=st.integers(0, 70), pend=st.integers(0, 600))
    def tick(self, dt: int, lag: int, pend: int) -> None:
        floor = (
            min([c for *_, c in self.network if c is not None], default=credit.TOLERANCE) - credit.TOLERANCE
        )
        if credit.compute(BASE, lag, pend) < floor:
            lag, pend = 2 * (BASE - floor), 0
        self.now += dt
        self.apply(self.core.on_tick(self.now, lag, pend))

    @rule(commit_old=st.booleans())
    def disconnect_and_resume(self, commit_old: bool) -> None:
        if commit_old:  # the process-wide ledger batcher may still commit rows of the dead connection
            self.ledgered.update(self.pending)
        self.now += 50
        self.connect(resume=True)

    @precondition(lambda self: self.closed is None)
    @rule()
    def superseded_then_reconnect(self) -> None:
        self.apply(self.core.on_superseded(self.epoch + 1))
        assert self.closed is not None and self.closed.code == CloseCode.SUPERSEDED
        self.ledgered.update(self.pending)
        self.connect(resume=True)

    # --- invariants ----------------------------------------------------------------------------

    @invariant()
    def ack_never_ahead_of_ledger(self) -> None:
        assert self.core.ack_seq <= self.core.ledger_seq
        assert all(s in self.ledgered for s in range(1, self.core.ledger_seq + 1))

    @invariant()
    def reorder_buffer_is_bounded(self) -> None:
        assert len(self.core._reorder) <= self.core.reorder_max

    def teardown(self) -> None:
        if self.closed is not None:
            self.connect(resume=True)
        final_seq = self.rec.last_sent
        self.apply(self.core.on_end(final_seq, self.now))
        for _ in range(200):
            if self.closed is not None:
                break
            while self.network and self.closed is None:
                self.deliver(self.network.pop(0))
            if self.pending:
                batch, self.pending = self.pending, []
                self.ledgered.update(batch)
                self.apply(self.core.on_ledgered(batch))
            self.now += END_WAIT_MS
            self.apply(self.core.on_tick(self.now, 0, 0))
        assert self.closed == Close(1000, "ended"), self.closed
        assert (self.acks[-1] == final_seq) if final_seq else not self.acks
        assert self.stored_ever == set(self.rec.sent), "loss or phantom store"
        assert set(range(1, final_seq + 1)) <= self.ledgered


IngestMachine.TestCase.settings = settings(
    max_examples=1_200,
    stateful_step_count=40,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large, HealthCheck.filter_too_much],
)
TestIngestCoreStateMachine = IngestMachine.TestCase
