"""IngestCore example-based tests: every close-code path plus the ack/nack/credit/resume rules (spec §6.4–6.5)."""

import pytest

from chartwire.ws import credit
from chartwire.ws.actions import (
    Ack,
    Close,
    Nack,
    Rehydrate,
    Send,
    SendCredit,
    Store,
    Transition,
)
from chartwire.ws.core import ACK_EVERY_CHUNKS, ACK_EVERY_MS, END_WAIT_MS, IngestCore

BASE = 50


def hello(core: IngestCore, *, ack=0, resume=False, last_sent=None, epoch=1, now=0, **kw):
    return core.on_hello(
        ack_seq_from_store=ack, resume=resume, last_sent_seq=last_sent, epoch=epoch, now_ms=now, **kw
    )


def fresh(now=0, **kw) -> IngestCore:
    core = IngestCore(now_ms=now, credit_base=BASE, session_id="sess-1", **kw)
    hello(core, now=now)
    return core


def closes(actions) -> list[Close]:
    return [a for a in actions if isinstance(a, Close)]


def stores(actions) -> list[int]:
    return [a.seq for a in actions if isinstance(a, Store)]


def sends(actions, t: str) -> list[dict]:
    return [a.msg for a in actions if isinstance(a, Send) and a.msg.get("t") == t]


def feed(core: IngestCore, seqs, now=0):
    out = []
    for s in seqs:
        out += core.on_chunk(s, (s - 1) * 200, 0, now)
    return out


# --- hello / welcome --------------------------------------------------------------------


def test_hello_emits_transition_and_welcome():
    core = IngestCore(now_ms=5, credit_base=BASE, session_id="sess-1")
    out = hello(core, epoch=7, now=5)
    assert out == [
        Transition("recording"),
        Send({"t": "welcome", "session_id": "sess-1", "epoch": 7, "ack_seq": 0, "credit": BASE, "heartbeat_ms": 15000, "missing": []}),
    ]  # fmt: skip
    assert core.epoch == 7 and core.ack_seq == 0 and core.ledger_seq == 0


def test_hello_without_hot_state_emits_rehydrate_first():
    core = IngestCore(now_ms=0, credit_base=BASE)
    out = hello(core, ack=12, hot_state_present=False)
    assert out[0] == Rehydrate()
    assert sends(out, "welcome")[0]["ack_seq"] == 12


def test_resume_welcome_lists_missing_ranges_excluding_ledgered_rows():
    core = IngestCore(now_ms=0, credit_base=BASE)
    out = hello(core, ack=10, resume=True, last_sent=20, ledgered_after_ack=[12, 13, 17])
    welcome = sends(out, "welcome")[0]
    assert welcome["ack_seq"] == 10
    assert welcome["missing"] == [[11, 11], [14, 16], [18, 20]]
    # 12, 13 and 17 are durable already: once 11 lands the ledger jumps to 13
    feed(core, [11])
    assert core.on_ledgered([11]) == []
    assert core.ledger_seq == 13
    assert core.on_tick(ACK_EVERY_MS, 0, 0) == [Ack(13, BASE)]


def test_resume_welcome_acknowledges_rows_already_contiguous_in_the_ledger():
    core = IngestCore(now_ms=0, credit_base=BASE)
    out = hello(core, ack=40, resume=True, last_sent=52, ledgered_after_ack=[41, 42, 43, 45, 46, 50])
    welcome = sends(out, "welcome")[0]
    assert welcome["ack_seq"] == 43 and core.ack_seq == core.ledger_seq == 43
    assert welcome["missing"] == [[44, 44], [47, 49], [51, 52]]
    assert stores(feed(core, [44, 47, 48, 49, 51, 52], now=10)) == [44, 47, 48, 49, 51, 52]
    assert core.on_ledgered([44]) == []  # 45, 46 were durable already → ledger 46, but < 8 chunks / < 100 ms
    assert core.ledger_seq == 46
    assert core.on_ledgered([47, 48, 49, 51, 52]) == [Ack(52, BASE)]  # 52 − 43 ≥ 8 chunks


def test_resume_without_gap_has_no_missing():
    core = IngestCore(now_ms=0, credit_base=BASE)
    out = hello(core, ack=30, resume=True, last_sent=30)
    assert sends(out, "welcome")[0]["missing"] == []
    assert core.outstanding == 0


def test_resume_gap_beyond_ring_buffer_closes_4008_and_ends_session():
    core = IngestCore(now_ms=0, credit_base=BASE)
    out = hello(core, ack=0, resume=True, last_sent=credit.RING_BUFFER_CHUNKS + 1)
    assert sends(out, "error")[0]["code"] == 4008
    assert Transition("ended") in out
    assert closes(out) == [Close(4008, "seq_gap_unrecoverable")]
    assert core.on_chunk(1, 0, 0, 1) == []  # inert after close


def test_resume_gap_exactly_ring_buffer_is_recoverable():
    core = IngestCore(now_ms=0, credit_base=BASE)
    out = hello(core, ack=0, resume=True, last_sent=credit.RING_BUFFER_CHUNKS)
    assert sends(out, "welcome")[0]["missing"] == [[1, credit.RING_BUFFER_CHUNKS]]


# --- sequencing -------------------------------------------------------------------------


def test_in_order_chunks_are_stored_immediately():
    core = fresh()
    assert core.on_chunk(1, 0, 0, 1) == [Store(1, 0, 0)]
    assert core.on_chunk(2, 200, 1, 2) == [Store(2, 200, 1)]
    assert core.contig_seq == 2 and core.stats.received == 2


def test_out_of_order_chunk_is_buffered_and_nacked_then_drained_in_order():
    core = fresh()
    feed(core, [1])
    assert core.on_chunk(4, 600, 0, 10) == [Nack(((2, 3),))]
    assert core.on_chunk(3, 400, 0, 11) == []  # same missing set within 100 ms: nack suppressed
    assert core.on_chunk(2, 200, 0, 12) == [Store(2, 200, 0), Store(3, 400, 0), Store(4, 600, 0)]
    assert core.stats.reordered == 2 and core.stats.nacks == 1


def test_nack_repeats_after_100ms_or_when_missing_set_changes():
    core = fresh()
    feed(core, [1])
    assert core.on_chunk(3, 400, 0, 10) == [Nack(((2, 2),))]
    assert core.on_chunk(5, 800, 0, 20) == [Nack(((2, 2), (4, 4)))]
    assert core.on_chunk(7, 1200, 0, 20 + ACK_EVERY_MS) == [Nack(((2, 2), (4, 4), (6, 6)))]
    assert core.on_chunk(9, 1600, 0, 21 + ACK_EVERY_MS) == [Nack(((2, 2), (4, 4), (6, 6), (8, 8)))]


def test_reorder_buffer_overflow_drops_chunk_and_keeps_nacking():
    core = fresh(reorder_max=2)
    feed(core, [1])
    feed(core, [3, 4], now=10)
    out = core.on_chunk(5, 800, 0, 200)
    assert core.stats.dropped == 1
    assert out == [Nack(((2, 2), (5, 5)))]
    assert stores(feed(core, [2], now=300)) == [2, 3, 4]
    assert stores(feed(core, [5], now=301)) == [5]


def test_duplicate_below_ack_re_sends_ack_and_is_counted():
    core = fresh()
    feed(core, [1, 2, 3])
    core.on_ledgered([1, 2, 3])
    core.on_tick(ACK_EVERY_MS, 0, 0)
    assert core.ack_seq == 3
    assert core.on_chunk(2, 200, 0, 150) == [Ack(3, BASE)]
    assert core.stats.duplicates == 1


def test_duplicate_between_ack_and_contig_is_silently_dropped():
    core = fresh()
    feed(core, [1, 2, 3])
    assert core.on_chunk(2, 200, 0, 5) == []
    assert core.on_chunk(3, 400, 0, 5) == []
    assert core.stats.duplicates == 2 and core.stats.stored == 0
    assert core.on_stored(1) == [] and core.stats.stored == 1


def test_duplicate_of_buffered_chunk_is_dropped():
    core = fresh()
    feed(core, [1, 3])
    assert core.on_chunk(3, 400, 0, 5) == []
    assert core.stats.duplicates == 1


# --- ack rules --------------------------------------------------------------------------


def test_ack_only_after_ledger_commit_and_ledger_is_contiguous():
    core = fresh()
    feed(core, [1, 2, 3, 4])
    assert core.on_ledgered([2, 3, 4]) == []  # 1 is missing → nothing durable in order
    assert core.ledger_seq == 0
    assert core.on_ledgered([1]) == []  # ledger 4, but < 8 chunks and < 100 ms
    assert core.ledger_seq == 4 and core.ack_seq == 0


def test_ack_after_eight_ledgered_chunks():
    core = fresh()
    feed(core, range(1, 10))
    assert core.on_ledgered(range(1, 8)) == []
    assert core.on_ledgered([8]) == [Ack(8, BASE)]
    assert core.on_ledgered([9]) == []
    assert core.stats.acks == 1


def test_ack_after_100ms_since_last_ack_on_tick():
    core = fresh()
    feed(core, [1, 2])
    core.on_ledgered([1, 2])
    assert core.on_tick(ACK_EVERY_MS - 1, 0, 0) == []
    assert core.on_tick(ACK_EVERY_MS, 0, 0) == [Ack(2, BASE)]
    assert core.on_tick(ACK_EVERY_MS + 1, 0, 0) == []  # nothing new to ack


def test_ack_immediately_when_credit_below_ten():
    core = fresh()
    core.on_tick(1, stt_lag=2 * (BASE - 5), node_pending=0)  # credit 5
    feed(core, [1], now=2)
    assert core.on_ledgered([1]) == [Ack(1, 5)]


def test_ack_is_never_ahead_of_ledger_and_is_monotone():
    core = fresh()
    feed(core, range(1, 20))
    for s in range(1, 20):
        for a in core.on_ledgered([s]) + core.on_tick(s * 60, 0, 0):
            if isinstance(a, Ack):
                assert a.ack_seq <= core.ledger_seq
        assert core.ack_seq <= core.ledger_seq
    assert core.on_tick(19 * 60 + ACK_EVERY_MS, 0, 0) == [Ack(19, BASE)]
    assert core.stats.acks == len(
        {a for a in range(1, 20) if a % 2 == 0 or a == 19}
    )  # one ack per 100 ms window


# --- credit -----------------------------------------------------------------------------


def test_credit_change_over_25_percent_is_sent_immediately():
    core = fresh()
    assert core.on_tick(200, stt_lag=20, node_pending=0) == []  # 50 → 40 (20 %)
    assert core.on_tick(400, stt_lag=30, node_pending=0) == [SendCredit(35)]  # vs advertised 50: 30 %
    assert core.advertised == 35
    assert core.on_tick(600, stt_lag=0, node_pending=0) == [SendCredit(50)]


def test_credit_violation_closes_4009_with_plus_twenty_tolerance():
    core = fresh()
    ok = feed(core, range(1, BASE + credit.TOLERANCE + 1))
    assert len(stores(ok)) == BASE + credit.TOLERANCE
    out = core.on_chunk(BASE + credit.TOLERANCE + 1, 0, 0, 1)
    assert sends(out, "error")[0]["code"] == 4009
    assert closes(out) == [Close(4009, "credit_violation")]
    assert core.on_chunk(1, 0, 0, 2) == []


def test_credit_zero_for_two_seconds_sends_pause_once():
    core = fresh()
    assert core.on_tick(200, stt_lag=1000, node_pending=0) == [SendCredit(0)]
    assert sends(core.on_tick(2_200, stt_lag=1000, node_pending=0), "pause") == []
    assert sends(core.on_tick(2_201, stt_lag=1000, node_pending=0), "pause") == [
        {"t": "pause", "reason": "stt_lag", "retry_ms": 2000}
    ]
    assert sends(core.on_tick(2_400, stt_lag=1000, node_pending=0), "pause") == []
    assert core.on_tick(2_600, stt_lag=0, node_pending=0) == [SendCredit(BASE)]
    core.on_tick(2_800, stt_lag=1000, node_pending=0)
    assert sends(core.on_tick(4_801, stt_lag=1000, node_pending=0), "pause")  # timer restarted


# --- end --------------------------------------------------------------------------------


def test_end_waits_for_ledger_then_transitions_bye_close_1000():
    core = fresh()
    feed(core, [1, 2, 3])
    assert core.on_end(3, 50) == []
    assert core.on_ledgered([1, 2]) == []
    out = core.on_ledgered([3])
    assert out == [
        Ack(3, BASE),
        Transition("ended"),
        Send({"t": "bye", "reason": "ended", "ack_seq": 3}),
        Close(1000, "ended"),
    ]
    assert core.closed and core.on_tick(100, 0, 0) == []


def test_end_with_missing_chunks_nacks_now_and_again_after_ten_seconds():
    core = fresh()
    feed(core, [1, 2])
    assert core.on_end(4, 10) == [Nack(((3, 4),))]
    assert core.on_tick(10 + END_WAIT_MS - 1, 0, 0) == []
    assert core.on_tick(10 + END_WAIT_MS, 0, 0) == [Nack(((3, 4),))]
    feed(core, [3, 4], now=10 + END_WAIT_MS + 1)
    assert closes(core.on_ledgered([1, 2, 3, 4]))[0].code == 1000


def test_end_all_received_but_ledger_lagging_does_not_nack():
    core = fresh()
    feed(core, [1, 2])
    assert core.on_end(2, 10) == []
    assert core.on_tick(10 + END_WAIT_MS, 0, 0) == []


def test_end_with_zero_chunks_finishes_immediately():
    core = fresh()
    out = core.on_end(0, 1)
    assert out[0] == Transition("ended") and closes(out)[0].code == 1000


def test_end_below_received_seqs_is_a_protocol_error_4010():
    core = fresh()
    feed(core, [1, 2, 3])
    out = core.on_end(2, 1)
    assert sends(out, "error")[0]["code"] == 4010 and closes(out)[0].code == 4010


def test_chunk_beyond_final_seq_closes_4012():
    core = fresh()
    feed(core, [1])
    core.on_end(2, 1)
    assert stores(core.on_chunk(2, 200, 0, 2)) == [2]
    out = core.on_chunk(3, 400, 0, 3)
    assert sends(out, "error")[0]["code"] == 4012 and closes(out) == [Close(4012, "session_ended")]


# --- heartbeat / superseded / consent / drain ------------------------------------------


def test_heartbeat_ping_and_close_4000_after_two_missed_pongs():
    core = fresh()
    assert core.on_tick(14_999, 0, 0) == []
    assert sends(core.on_tick(15_000, 0, 0), "ping") == [{"t": "ping", "ts": 15_000}]
    assert core.on_pong(15_100) == []
    assert sends(core.on_tick(30_000, 0, 0), "ping")
    assert sends(core.on_tick(45_000, 0, 0), "ping")  # one unanswered → still pinging
    assert core.on_tick(60_000, 0, 0) == [Close(4000, "heartbeat_timeout")]
    assert core.closed and core.on_chunk(1, 0, 0, 60_001) == []


def test_superseded_by_newer_epoch_sends_bye_and_closes_4409():
    core = fresh()
    feed(core, [1, 2])
    core.on_ledgered([1, 2])
    assert core.on_superseded(1) == []  # same epoch: stale notification
    out = core.on_superseded(2)
    assert out == [
        Ack(2, BASE),
        Send({"t": "bye", "reason": "superseded", "ack_seq": 2}),
        Close(4409, "superseded"),
    ]
    assert core.on_chunk(3, 400, 0, 1) == []


def test_consent_revoked_sends_bye_and_closes_4011():
    core = fresh()
    out = core.on_consent_revoked()
    assert out == [
        Send({"t": "bye", "reason": "consent_revoked", "ack_seq": 0}),
        Close(4011, "consent_revoked"),
    ]
    assert core.on_consent_revoked() == []


def test_drain_sends_one_bye_keeps_acking_then_closes_1012_when_flushed():
    core = fresh()
    feed(core, [1, 2, 3])
    assert core.on_drain(1_000) == [Send({"t": "bye", "reason": "drain", "ack_seq": 0})]
    assert core.on_drain(1_001) == []  # idempotent: one bye per connection
    assert core.on_ledgered([1, 2]) == [Ack(2, BASE)]  # acks keep flowing after bye (≥100 ms rule)
    assert core.on_ledgered([3]) == [Ack(3, BASE), Close(1012, "drain")]
    assert core.closed and core.stats.acks == 2


def test_drain_still_stores_in_flight_chunks_and_waits_for_them():
    core = fresh()
    feed(core, [1])
    core.on_drain(1_000)
    assert core.on_chunk(2, 200, 0, 1_010) == [Store(2, 200, 0)]
    assert core.on_ledgered([1]) == [Ack(1, BASE)]  # chunk 2 stored but not durable: stay open
    assert core.on_ledgered([2]) == [Ack(2, BASE), Close(1012, "drain")]


def test_drain_deadline_closes_even_if_ledger_is_stuck_and_stops_pinging():
    core = fresh()
    feed(core, [1])
    core.on_drain(1_000)
    assert core.on_tick(20_999, 0, 0) == []  # no ping after bye
    assert core.on_tick(21_000, 0, 0) == [Close(1012, "drain")]  # nothing durable → no ack, no second bye
    assert core.closed and core.on_ledgered([1]) == []


def test_drain_with_nothing_pending_closes_at_once():
    core = fresh()
    assert core.on_drain(5) == [Send({"t": "bye", "reason": "drain", "ack_seq": 0}), Close(1012, "drain")]


def test_ack_batching_constant_matches_spec():
    assert ACK_EVERY_CHUNKS == 8 and ACK_EVERY_MS == 100


@pytest.mark.parametrize("method", ["on_end", "on_tick", "on_superseded", "on_drain"])
def test_closed_core_is_inert(method):
    core = fresh()
    core.on_consent_revoked()
    args = {"on_end": (1, 1), "on_tick": (1, 0, 0), "on_superseded": (9,), "on_drain": (1,)}[method]
    assert getattr(core, method)(*args) == []
