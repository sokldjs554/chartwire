"""WatchCore example-based tests: Subscribe-then-Replay ordering, dedup, gap filling, 4013/4000 (spec §6.5, §6.7)."""

from chartwire.ws.actions import Close, Replay, Send, Subscribe
from chartwire.ws.core import WATCH_PENDING_MAX, WatchCore


def final(seq: int) -> dict:
    return {"t": "transcript.final", "seq": seq, "speaker": "patient", "t_start_ms": seq * 1000, "t_end_ms": seq * 1000 + 900, "text": "합성 발화", "confidence": 0.9, "segment_id": seq + 1}  # fmt: skip


def alert(rid: str, seq: int = 0) -> dict:
    return {"t": "risk.alert", "risk_event_id": rid, "category": "self_harm", "severity": 3, "segment_seq": seq, "span": [0, 2]}  # fmt: skip


def sent_final_seqs(actions) -> list[int]:
    return [a.msg["seq"] for a in actions if isinstance(a, Send) and a.msg.get("t") == "transcript.final"]


def test_hello_subscribes_before_replay_and_welcomes():
    core = WatchCore(now_ms=0, session_id="sess-1")
    out = core.on_hello(None, state="recording", last_final_seq=4)
    assert out == [
        Subscribe(),
        Send({"t": "welcome", "session_id": "sess-1", "state": "recording", "last_final_seq": 4}),
        Replay(-1),
    ]
    assert core.replaying


def test_hello_from_seq_replays_after_previous_seq():
    core = WatchCore(now_ms=0)
    assert core.on_hello(12)[-1] == Replay(11)
    assert core.on_replay_batch([final(10), final(11), final(12), final(13)]) == [
        Send(final(12)),
        Send(final(13)),
    ]


def test_replay_then_live_without_duplicates():
    core = WatchCore(now_ms=0)
    core.on_hello(None)
    assert sent_final_seqs(core.on_replay_batch([final(0), final(1), final(2)])) == [0, 1, 2]
    assert core.on_live_event(final(2)) == []  # published before the replay query ran: duplicate
    assert core.on_live_event(final(3)) == []  # still replaying: held back
    assert sent_final_seqs(core.on_replay_done()) == [3]
    assert sent_final_seqs(core.on_live_event(final(4))) == [4]
    assert core.on_live_event(final(4)) == [] and core.on_live_event(final(1)) == []


def test_live_gap_triggers_replay_and_holds_the_final():
    core = WatchCore(now_ms=0)
    core.on_hello(None)
    core.on_replay_batch([final(0)])
    assert core.on_replay_done() == []
    assert core.on_live_event(final(3)) == [Replay(0)]
    assert core.replaying
    assert sent_final_seqs(core.on_replay_batch([final(1), final(2), final(3)])) == [1, 2, 3]
    assert core.on_replay_done() == []
    assert sent_final_seqs(core.on_live_event(final(4))) == [4]


def test_replay_is_the_truth_for_gaps_in_the_database():
    core = WatchCore(now_ms=0)
    core.on_hello(None)
    core.on_replay_done()
    core.on_live_event(final(5))
    # the database really has no seq 3: the replay returns 1, 2, 4 and the held-back 5 flushes after it
    assert sent_final_seqs(core.on_replay_batch([final(1), final(2), final(4)])) == [1, 2, 4]
    assert sent_final_seqs(core.on_replay_done()) == [5]
    assert not core.replaying


def test_stale_pending_entries_are_pruned_after_replay():
    core = WatchCore(now_ms=0)
    core.on_hello(None)
    core.on_live_event(final(1))
    core.on_replay_batch([final(0), final(1), final(2)])
    assert core.on_replay_done() == []
    assert not core.replaying and core.last_final_seq == 2


def test_alerts_are_deduplicated_across_replay_and_live():
    core = WatchCore(now_ms=0)
    core.on_hello(None)
    assert core.on_replay_batch([alert("r1"), final(0)]) == [Send(alert("r1")), Send(final(0))]
    assert core.on_live_event(alert("r1")) == []
    assert core.on_live_event(alert("r2", 0)) == [Send(alert("r2", 0))]  # alerts pass even during replay
    assert core.on_live_event(alert("r2", 0)) == []


def test_other_events_pass_through_unchanged():
    core = WatchCore(now_ms=0)
    core.on_hello(None)
    for ev in (
        {"t": "transcript.partial", "from_seq": 3, "text": "ㅈ"},
        {"t": "session.state", "state": "ended"},
        {"t": "viewer.presence", "count": 2},
    ):
        assert core.on_live_event(ev) == [Send(ev)]  # fmt: skip


def test_pending_overflow_closes_4013():
    core = WatchCore(now_ms=0)
    core.on_hello(None)
    out = []
    for seq in range(1, WATCH_PENDING_MAX + 2):
        out = core.on_live_event(final(seq))
    assert out == [Close(4013, "viewer_too_slow")]
    assert core.closed and core.on_live_event(final(1)) == []


def test_heartbeat_closes_4000_and_core_goes_inert():
    core = WatchCore(now_ms=0)
    core.on_hello(None)
    assert core.on_tick(15_000) == [Send({"t": "ping", "ts": 15_000})]
    core.on_pong(15_001)
    assert core.on_tick(30_000) == [Send({"t": "ping", "ts": 30_000})]
    assert core.on_tick(45_000) == [Send({"t": "ping", "ts": 45_000})]
    assert core.on_tick(60_000) == [Close(4000, "heartbeat_timeout")]
    assert core.closed and core.on_tick(75_000) == []
