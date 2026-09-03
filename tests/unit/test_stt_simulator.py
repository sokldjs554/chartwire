"""ScriptedSimulator timeline correctness (spec §10.4)."""

from __future__ import annotations

import random
from itertools import pairwise
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from chartwire.stt.base import Chunk, Final, Partial, SessionInfo, SttEvent
from chartwire.stt.scripts_io import ScriptError, load_script, script_path
from chartwire.stt.simulator import (
    LATENCY_MAX_MS,
    LATENCY_MIN_MS,
    ScriptedSimulator,
    Timeline,
    gaussian_latency_ms,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "scripts"


def session(ref: str = "t01", chunk_ms: int = 200) -> SessionInfo:
    return SessionInfo(session_id=uuid4(), tenant_id=uuid4(), script_ref=ref, chunk_ms=chunk_ms)


def chunk(sid: UUID, seq: int, offset_ms: int) -> Chunk:
    return Chunk(session_id=sid, seq=seq, offset_ms=offset_ms, flags=0, payload=b"\x00" * 16)


async def run_all(
    sim: ScriptedSimulator, info: SessionInfo, total_ms: int
) -> list[tuple[Chunk, list[SttEvent]]]:
    stream = await sim.open(info)
    out = []
    for i in range(total_ms // info.chunk_ms + 1):
        c = chunk(info.session_id, i + 1, i * info.chunk_ms)
        out.append((c, await stream.feed(c)))
    return out


@pytest.mark.parametrize(("ref", "chunk_ms"), [("t01", 200), ("t02", 200), ("t03", 100), ("t01", 300)])
async def test_finals_appear_exactly_when_t_end_falls_in_the_window(ref: str, chunk_ms: int):
    script = load_script(script_path(FIXTURES, ref))
    sim = ScriptedSimulator(FIXTURES, latency=None)
    info = session(ref, chunk_ms)
    trace = await run_all(sim, info, script.total_ms)
    finals = [(c, e) for c, evs in trace for e in evs if isinstance(e, Final)]
    assert [e.text for _, e in finals] == [u.text for u in script.utterances]
    for c, f in finals:
        assert c.offset_ms <= f.t_end_ms < c.offset_ms + chunk_ms
        assert f.seq_end == c.seq and f.seq_start <= f.seq_end
        assert 0.86 <= f.confidence <= 0.99
    assert [f.speaker for _, f in finals] == [u.speaker for u in script.utterances]


async def test_partials_every_second_chunk_with_monotone_prefixes():
    script = load_script(script_path(FIXTURES, "t01"))
    sim = ScriptedSimulator(FIXTURES, latency=None)
    info = session("t01")
    trace = await run_all(sim, info, script.total_ms)
    partial_seqs = [c.seq for c, evs in trace for e in evs if isinstance(e, Partial)]
    assert partial_seqs and all(seq % 2 == 0 for seq in partial_seqs)
    # prefixes of one utterance grow monotonically and are prefixes of the final text
    by_from: dict[int, list[str]] = {}
    for _, evs in trace:
        for e in evs:
            if isinstance(e, Partial):
                by_from.setdefault(e.from_seq, []).append(e.text)
    texts = {u.text for u in script.utterances}
    assert len(by_from) == len(script.utterances)
    for prefixes in by_from.values():
        assert all(len(a) < len(b) and b.startswith(a) for a, b in pairwise(prefixes))
        assert any(t.startswith(prefixes[-1]) for t in texts)


async def test_partial_from_seq_matches_the_final_seq_start():
    script = load_script(script_path(FIXTURES, "t01"))
    sim = ScriptedSimulator(FIXTURES, latency=None)
    trace = await run_all(sim, session("t01"), script.total_ms)
    starts = {e.text: e.seq_start for _, evs in trace for e in evs if isinstance(e, Final)}
    for _, evs in trace:
        for e in evs:
            if isinstance(e, Partial):
                full = next(t for t in starts if t.startswith(e.text))
                assert starts[full] == e.from_seq


async def test_flush_emits_remaining_finals_and_then_nothing():
    sim = ScriptedSimulator(FIXTURES, latency=None)
    info = session("t01")
    stream = await sim.open(info)
    got: list[Final] = []
    for i in range(10):  # stop at 2 s: only the first utterance (t_end 1500) is released
        got += [e for e in await stream.feed(chunk(info.session_id, i + 1, i * 200)) if isinstance(e, Final)]
    assert [f.text for f in got] == ["안녕하세요 어떻게 지내셨어요"]
    rest = await stream.flush()
    assert all(isinstance(e, Final) for e in rest)
    assert [e.text for e in rest if isinstance(e, Final)] == [
        u.text for u in load_script(FIXTURES / "t01.json").utterances[1:]
    ]
    assert all(e.seq_end == 10 for e in rest if isinstance(e, Final))
    assert await stream.flush() == []


async def test_duplicate_and_gapped_chunks_never_lose_or_duplicate_finals():
    sim = ScriptedSimulator(FIXTURES, latency=None)
    info = session("t01")
    stream = await sim.open(info)
    texts: list[str] = []
    seqs = [1, 2, 3, 3, 4, 20, 21, 21, 30, 44]  # duplicates and a gap (rebuild scenarios, §7.4)
    for seq in seqs:
        texts += [
            e.text
            for e in await stream.feed(chunk(info.session_id, seq, (seq - 1) * 200))
            if isinstance(e, Final)
        ]
    texts += [e.text for e in await stream.flush() if isinstance(e, Final)]
    assert texts == [u.text for u in load_script(FIXTURES / "t01.json").utterances]


async def test_latency_is_seeded_clamped_and_awaited_once_per_chunk_with_finals():
    slept: list[float] = []

    async def fake_sleep(s: float) -> None:
        slept.append(s)

    sim = ScriptedSimulator(FIXTURES, seed=7, sleep=fake_sleep)
    script = load_script(FIXTURES / "t01.json")
    trace = await run_all(sim, session("t01"), script.total_ms)
    n_final_chunks = sum(1 for _, evs in trace if any(isinstance(e, Final) for e in evs))
    assert len(slept) == n_final_chunks == len(script.utterances)
    assert all(LATENCY_MIN_MS / 1000 <= s <= LATENCY_MAX_MS / 1000 for s in slept)

    rng = random.Random(1)
    draws = [gaussian_latency_ms(rng) for _ in range(2000)]
    assert min(draws) >= LATENCY_MIN_MS and max(draws) <= LATENCY_MAX_MS
    assert 110 < sum(draws) / len(draws) < 130


async def test_same_seed_and_session_reproduce_confidences():
    sid = uuid4()
    info = SessionInfo(sid, uuid4(), "t01", 200)

    async def confs() -> list[float]:
        stream = await ScriptedSimulator(FIXTURES, seed=3, latency=None).open(info)
        return [e.confidence for e in await stream.flush() if isinstance(e, Final)]

    assert await confs() == await confs()


async def test_timeline_is_cached_per_script_and_chunk_size():
    sim = ScriptedSimulator(FIXTURES, latency=None)
    a = await sim.open(session("t01", 200))
    b = await sim.open(session("t01", 200))
    c = await sim.open(session("t01", 100))
    assert a._tl is b._tl and a._tl is not c._tl


async def test_open_rejects_missing_script_ref_or_file():
    sim = ScriptedSimulator(FIXTURES, latency=None)
    with pytest.raises(ScriptError):
        await sim.open(SessionInfo(uuid4(), uuid4(), None))
    with pytest.raises(ScriptError):
        await sim.open(session("does-not-exist"))
    with pytest.raises(ScriptError):
        await sim.open(session("t01", 0))


def test_timeline_index_is_consistent_with_a_brute_force_scan():
    script = load_script(FIXTURES / "t01.json")
    tl = Timeline.build(script, 200)
    for w in range(len(tl.finals_before)):
        p = (w + 1) * 200
        assert tl.finals_before[w] == sum(1 for u in script.utterances if u.t_end_ms < p)
        expect = next((u.idx for u in script.utterances if u.t_start_ms <= p < u.t_end_ms), -1)
        assert tl.current[w] == expect
    assert tl.finals_released_by(10_000) == len(script.utterances)
    assert tl.current_at(10_000) == -1 and tl.finals_released_by(-1) == 0
