"""Scripted STT simulator (spec §10.4).

The recorder streams seeded pseudo-random audio; the transcript comes from the script's timeline,
keyed by ``(script_ref, offset_ms)``. Per chunk the stream computes the window
``[offset_ms, offset_ms + chunk_ms)`` and

* emits a :class:`Final` for every utterance whose ``t_end_ms`` falls inside the window, after a
  seeded provider-latency sleep drawn from N(120, 30) ms clamped to [30, 300] ms;
* on every second chunk, emits a :class:`Partial` for the utterance that contains
  ``offset_ms + chunk_ms`` with the character prefix proportional to the elapsed fraction.

The timeline is pre-indexed per window so ``feed()`` is O(1) plus the events it returns; the
stt-worker must never be the bottleneck in load scenario A (§7.4). Indexing is by *prefix counts*,
so a chunk that arrives after a gap still releases every final whose ``t_end`` has passed —
nothing is ever lost, and a duplicated chunk produces nothing twice.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from chartwire.stt.base import Chunk, Final, Partial, SessionInfo, SttEvent
from chartwire.stt.scripts_io import Script, ScriptError, load_script, script_path

LatencyModel = Callable[[random.Random], float]
"""Draws one provider latency in milliseconds from the stream's seeded RNG."""

Sleep = Callable[[float], Awaitable[None]]

LATENCY_MEAN_MS = 120.0
LATENCY_SD_MS = 30.0
LATENCY_MIN_MS = 30.0
LATENCY_MAX_MS = 300.0


def gaussian_latency_ms(rng: random.Random) -> float:
    """N(120, 30) ms clamped to [30, 300] ms — the default provider-latency model."""
    return min(LATENCY_MAX_MS, max(LATENCY_MIN_MS, rng.gauss(LATENCY_MEAN_MS, LATENCY_SD_MS)))


@dataclass(frozen=True)
class TimedUtterance:
    speaker: str
    text: str
    t_start_ms: int
    t_end_ms: int


@dataclass(frozen=True)
class Timeline:
    """Per-window index of one script for one ``chunk_ms``.

    ``finals_before[w]`` — number of utterances whose ``t_end_ms < (w + 1) * chunk_ms``
    (i.e. finalised in window ``w`` or earlier).
    ``current[w]`` — index of the utterance containing ``(w + 1) * chunk_ms``, or ``-1``.
    """

    chunk_ms: int
    utterances: tuple[TimedUtterance, ...]
    finals_before: tuple[int, ...]
    current: tuple[int, ...]

    @classmethod
    def build(cls, script: Script, chunk_ms: int) -> Timeline:
        utts = tuple(TimedUtterance(u.speaker, u.text, u.t_start_ms, u.t_end_ms) for u in script.utterances)
        n = len(utts)
        windows = script.total_ms // chunk_ms + 2
        finals_before: list[int] = []
        current: list[int] = []
        j = k = 0
        for w in range(windows):
            p = (w + 1) * chunk_ms
            while j < n and utts[j].t_end_ms < p:
                j += 1
            finals_before.append(j)
            while k < n and utts[k].t_end_ms <= p:
                k += 1
            current.append(k if k < n and utts[k].t_start_ms <= p else -1)
        return cls(chunk_ms, utts, tuple(finals_before), tuple(current))

    def window(self, offset_ms: int) -> int:
        return offset_ms // self.chunk_ms

    def finals_released_by(self, w: int) -> int:
        if w < 0:
            return 0
        return self.finals_before[w] if w < len(self.finals_before) else len(self.utterances)

    def current_at(self, w: int) -> int:
        return self.current[w] if 0 <= w < len(self.current) else -1


class ScriptedStream:
    """One session's simulated stream. Not thread-safe; the worker owns one task per session."""

    def __init__(
        self,
        timeline: Timeline,
        rng: random.Random,
        latency: LatencyModel | None,
        sleep: Sleep,
    ) -> None:
        self._tl = timeline
        self._rng = rng
        self._latency = latency
        self._sleep = sleep
        self._emitted = 0
        self._chunks_fed = 0
        self._last_seq = 0
        self._first_seq: dict[int, int] = {}

    async def feed(self, chunk: Chunk) -> list[SttEvent]:
        self._chunks_fed += 1
        self._last_seq = chunk.seq
        w = self._tl.window(chunk.offset_ms)
        p = chunk.offset_ms + self._tl.chunk_ms
        events: list[SttEvent] = []

        cur = self._tl.current_at(w)
        if cur >= 0:
            self._first_seq.setdefault(cur, chunk.seq)

        released = self._tl.finals_released_by(w)
        if released > self._emitted:
            if self._latency is not None:
                await self._sleep(self._latency(self._rng) / 1000.0)
            events.extend(self._finalize(released, chunk.seq))

        if cur >= 0 and self._chunks_fed % 2 == 0:
            partial = self._partial(cur, p)
            if partial is not None:
                events.append(partial)
        return events

    async def flush(self) -> list[SttEvent]:
        return list(self._finalize(len(self._tl.utterances), self._last_seq))

    def _finalize(self, upto: int, seq_end: int) -> list[SttEvent]:
        out: list[SttEvent] = []
        for idx in range(self._emitted, upto):
            u = self._tl.utterances[idx]
            out.append(
                Final(
                    seq_start=self._first_seq.get(idx, seq_end),
                    seq_end=seq_end,
                    speaker=u.speaker,  # type: ignore[arg-type]  # validated Literal by scripts_io
                    t_start_ms=u.t_start_ms,
                    t_end_ms=u.t_end_ms,
                    text=u.text,
                    confidence=round(self._rng.uniform(0.86, 0.99), 3),
                )
            )
        self._emitted = max(self._emitted, upto)
        return out

    def _partial(self, idx: int, p: int) -> Partial | None:
        u = self._tl.utterances[idx]
        fraction = (p - u.t_start_ms) / (u.t_end_ms - u.t_start_ms)
        n_chars = int(len(u.text) * fraction)
        if n_chars < 1:
            return None
        return Partial(from_seq=self._first_seq[idx], text=u.text[:n_chars])


class ScriptedSimulator:
    """``SttAdapter`` that replays ``<scripts_dir>/<script_ref>.json`` (spec §3.1, §10.4).

    ``latency=None`` disables the provider-latency sleep (tests, offline eval); ``seed`` makes
    latencies and confidences reproducible per session. Parsed timelines are cached per
    ``(script_ref, chunk_ms)`` so 200 concurrent sessions on the same 20 scripts parse each once.
    """

    def __init__(
        self,
        scripts_dir: Path | str,
        *,
        seed: int = 0,
        latency: LatencyModel | None = gaussian_latency_ms,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._scripts_dir = Path(scripts_dir)
        self._seed = seed
        self._latency = latency
        self._sleep = sleep
        self._cache: dict[tuple[str, int], Timeline] = {}

    async def open(self, session: SessionInfo) -> ScriptedStream:
        if session.script_ref is None:
            raise ScriptError("session has no script_ref; the simulator needs one")
        if session.chunk_ms <= 0:
            raise ScriptError(f"chunk_ms must be positive, got {session.chunk_ms}")
        timeline = self._timeline(session.script_ref, session.chunk_ms)
        rng = random.Random(f"{self._seed}:{session.session_id}")
        return ScriptedStream(timeline, rng, self._latency, self._sleep)

    def _timeline(self, script_ref: str, chunk_ms: int) -> Timeline:
        key = (script_ref, chunk_ms)
        tl = self._cache.get(key)
        if tl is None:
            tl = Timeline.build(load_script(script_path(self._scripts_dir, script_ref)), chunk_ms)
            self._cache[key] = tl
        return tl
