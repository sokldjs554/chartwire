"""Hypothesis stateful test for ``WatchCore`` (spec §6.5, §6.7).

The model owns a PostgreSQL-like list of committed finals and a Redis-like live queue. A final
is published live only after it was committed (as the stt-worker does), but the live queue
may be delivered late, out of order relative to the replay, or twice. Viewers reconnect at
random with ``from_seq``. Invariants per connection:

* ``Subscribe`` precedes ``Replay`` on hello
* finals sent to the viewer are strictly increasing by exactly one, starting at ``from_seq``
* an alert id is sent at most once
* at teardown, after draining replay and live, the viewer has every committed final ≥ from_seq
"""

from __future__ import annotations

from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, initialize, invariant, precondition, rule

from chartwire.ws.actions import Action, Close, Replay, Send, Subscribe
from chartwire.ws.core import WatchCore


def final(seq: int) -> dict:
    return {"t": "transcript.final", "seq": seq, "speaker": "patient", "t_start_ms": seq * 1000, "t_end_ms": seq * 1000 + 900, "text": "합성", "confidence": 0.9, "segment_id": seq + 1}  # fmt: skip


def alert(rid: int, seq: int) -> dict:
    return {"t": "risk.alert", "risk_event_id": f"r{rid}", "category": "self_harm", "severity": 3, "segment_seq": seq, "span": [0, 1]}  # fmt: skip


class WatchMachine(RuleBasedStateMachine):
    @initialize()
    def start(self) -> None:
        self.now = 0
        self.db: list[int] = []  # committed final seqs, contiguous from 0
        self.db_alerts: dict[int, int] = {}  # risk_event_id → segment seq
        self.live: list[dict] = []  # published but not yet delivered to this api node
        self.replay_rows: list[dict] | None = None  # snapshot served for the current Replay
        self.connect(from_seq=None)

    def connect(self, *, from_seq: int | None) -> None:
        self.core = WatchCore(now_ms=self.now, session_id="sess")
        self.from_seq = from_seq or 0
        self.delivered: list[int] = []
        self.alerts_seen: set[str] = set()
        self.closed: Close | None = None
        out = self.core.on_hello(from_seq)
        assert isinstance(out[0], Subscribe) and isinstance(out[-1], Replay), out
        self.apply(out)

    def apply(self, actions: list[Action]) -> None:
        for a in actions:
            if isinstance(a, Send) and a.msg.get("t") == "transcript.final":
                expected = self.delivered[-1] + 1 if self.delivered else self.from_seq
                assert a.msg["seq"] == expected, (a.msg["seq"], expected)
                assert a.msg["seq"] in self.db, "final the database never committed"
                self.delivered.append(a.msg["seq"])
            elif isinstance(a, Send) and a.msg.get("t") == "risk.alert":
                assert a.msg["risk_event_id"] not in self.alerts_seen, "duplicate alert"
                self.alerts_seen.add(a.msg["risk_event_id"])
            elif isinstance(a, Send) and a.msg.get("t") == "ping":
                self.apply(self.core.on_pong(self.now))
            elif isinstance(a, Replay):
                rows = [final(s) for s in self.db if s > a.after_seq]
                rows += [alert(r, s) for r, s in self.db_alerts.items() if s > a.after_seq]
                self.replay_rows = sorted(rows, key=lambda r: (r.get("seq", r.get("segment_seq")), r["t"]))
            elif isinstance(a, Close):
                self.closed = a

    # --- rules ---------------------------------------------------------------------------------

    @rule(n=st.integers(1, 5), with_alert=st.booleans())
    def commit_finals(self, n: int, with_alert: bool) -> None:
        for _ in range(n):
            seq = len(self.db)
            self.db.append(seq)
            self.live.append(final(seq))
            if with_alert:
                rid = len(self.db_alerts)
                self.db_alerts[rid] = seq
                self.live.append(alert(rid, seq))

    @precondition(lambda self: self.live and self.closed is None)
    @rule(data=st.data())
    def deliver_live(self, data: st.DataObject) -> None:
        idx = data.draw(st.integers(0, min(3, len(self.live) - 1)))  # mostly in order, sometimes late
        self.apply(self.core.on_live_event(self.live.pop(idx)))

    @precondition(lambda self: self.db and self.closed is None)
    @rule(data=st.data())
    def deliver_live_duplicate(self, data: st.DataObject) -> None:
        self.apply(self.core.on_live_event(final(data.draw(st.sampled_from(self.db)))))

    @precondition(lambda self: self.replay_rows is not None and self.closed is None)
    @rule(batch=st.integers(1, 4))
    def replay_step(self, batch: int) -> None:
        assert self.replay_rows is not None
        rows, self.replay_rows = self.replay_rows[:batch], self.replay_rows[batch:]
        self.apply(self.core.on_replay_batch(rows))
        if not self.replay_rows:
            self.replay_rows = None
            self.apply(self.core.on_replay_done())

    @rule(data=st.data())
    def reconnect(self, data: st.DataObject) -> None:
        self.now += 10
        self.live = []
        self.replay_rows = None
        self.connect(from_seq=data.draw(st.one_of(st.none(), st.integers(0, len(self.db) + 1))))

    @precondition(lambda self: self.closed is None)
    @rule(dt=st.integers(1, 1000))
    def tick(self, dt: int) -> None:
        self.now += dt
        self.apply(self.core.on_tick(self.now))

    @invariant()
    def delivered_finals_are_contiguous(self) -> None:
        assert self.delivered == list(range(self.from_seq, self.from_seq + len(self.delivered)))

    def teardown(self) -> None:
        for _ in range(50):
            if self.closed is not None:
                return
            while self.replay_rows is not None:
                self.replay_step(8)
            while self.live and self.closed is None:
                self.apply(self.core.on_live_event(self.live.pop(0)))
            if self.replay_rows is None and not self.live:
                break
        assert self.closed is None
        assert self.delivered == [s for s in self.db if s >= self.from_seq], "viewer missed a committed final"
        assert self.alerts_seen >= {f"r{r}" for r, s in self.db_alerts.items() if s >= self.from_seq}


WatchMachine.TestCase.settings = settings(
    max_examples=1_000,
    stateful_step_count=32,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large, HealthCheck.filter_too_much],
)
TestWatchCoreStateMachine = WatchMachine.TestCase
