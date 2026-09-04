"""Load-test scenario definitions (spec §11.2) — pure data, no I/O.

* **A** — N ∈ {50, 100, 200} recorder sessions, 200 ms chunks (5 chunks/s/session), one viewer each, 60 s.
* **B** — N = 50 against ``SlowStt(delay_ms=400)``: the only purpose is to show credit reaching 0, the
  recorder pausing, a bounded stream length and a flat api RSS.
* **C** — N = 100 with 20 % slow viewers (200 ms sleep per message): recorder ack p95 must not move.
* **D** — N = 100 chaos: every 10 s 10 % of recorder sockets are aborted without a close frame, the
  run's Redis database is flushed at t = 30 s, and the stt-worker is ``SIGSTOP``\ ped for 15 s at t = 40 s.
* **H** — the outbox bench (``chartwire outbox bench``); it lives in :mod:`chartwire.outbox.bench`.

The chaos schedule is a pure function of the scenario and duration so the unit tests pin it.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Literal

SttProvider = Literal["simulator", "slow"]
ChaosKind = Literal["kill_sockets", "flush_redis", "stt_sigstop", "stt_sigcont"]

A_SESSION_COUNTS: Final[tuple[int, ...]] = (50, 100, 200)
CHUNK_MS: Final = 200
CHUNKS_PER_S: Final = 1000 // CHUNK_MS


@dataclass(frozen=True)
class Scenario:
    name: str
    description: str
    sessions: int
    duration_s: int = 60
    viewers_per_session: int = 1
    stt_provider: SttProvider = "simulator"
    stt_slow_delay_ms: int = 0
    slow_viewer_fraction: float = 0.0
    slow_viewer_delay_s: float = 0.0
    kill_fraction: float = 0.0
    """Share of recorder sockets aborted (no close frame) at every ``kill_every_s`` tick."""
    kill_every_s: float = 0.0
    flush_redis_at_s: float | None = None
    stt_sigstop_at_s: float | None = None
    stt_sigstop_for_s: float = 0.0

    @property
    def chaos(self) -> bool:
        return bool(
            self.kill_every_s or self.flush_redis_at_s is not None or self.stt_sigstop_at_s is not None
        )

    def total_chunks(self, duration_s: int | None = None) -> int:
        """Chunks one recorder sends: ``duration × 5`` at 200 ms (§11.2 "5/s/session")."""
        return max(1, int((duration_s or self.duration_s) * CHUNKS_PER_S))

    def slow_viewers(self, n_sessions: int) -> int:
        """Number of viewers that sleep per message (scenario C: 20 % of ``n``)."""
        return round(n_sessions * self.slow_viewer_fraction)


SCENARIOS: Final[dict[str, Scenario]] = {
    "A": Scenario(
        "A",
        "N 세션 × 200 ms 청크 × 뷰어 1, 60 s — ack/final/alert 지연, 처리량, 자원",
        sessions=50,
    ),
    "B": Scenario(
        "B",
        "SlowStt 400 ms — credit → 0, pause, 스트림 길이 상한, api RSS 기울기",
        sessions=50,
        stt_provider="slow",
        stt_slow_delay_ms=400,
    ),
    "C": Scenario(
        "C",
        "느린 뷰어 20 % (메시지당 200 ms) — 녹음기 ack p95 는 A 와 같아야 한다",
        sessions=100,
        slow_viewer_fraction=0.2,
        slow_viewer_delay_s=0.2,
    ),
    "D": Scenario(
        "D",
        "카오스 — 10 s 마다 소켓 10 % 강제 종료, 30 s 에 Redis FLUSHDB, 40 s 에 stt-worker SIGSTOP 15 s",
        sessions=100,
        kill_fraction=0.10,
        kill_every_s=10.0,
        flush_redis_at_s=30.0,
        stt_sigstop_at_s=40.0,
        stt_sigstop_for_s=15.0,
    ),
}

RUN_ORDER: Final[tuple[str, ...]] = ("A", "B", "C", "D", "H")


@dataclass(frozen=True)
class ChaosEvent:
    at_s: float
    kind: ChaosKind
    fraction: float = 0.0


def chaos_schedule(scenario: Scenario, duration_s: int | None = None) -> list[ChaosEvent]:
    """Every chaos event of a run, ordered by time. Socket kills happen at 10, 20, … (never at 0 and
    never at the very end, where recorders are already sending ``end``)."""
    duration = duration_s or scenario.duration_s
    events: list[ChaosEvent] = []
    if scenario.kill_every_s > 0 and scenario.kill_fraction > 0:
        t = scenario.kill_every_s
        while t < duration:
            events.append(ChaosEvent(t, "kill_sockets", scenario.kill_fraction))
            t += scenario.kill_every_s
    if scenario.flush_redis_at_s is not None and scenario.flush_redis_at_s < duration:
        events.append(ChaosEvent(scenario.flush_redis_at_s, "flush_redis"))
    if scenario.stt_sigstop_at_s is not None and scenario.stt_sigstop_at_s < duration:
        events.append(ChaosEvent(scenario.stt_sigstop_at_s, "stt_sigstop"))
        events.append(ChaosEvent(scenario.stt_sigstop_at_s + scenario.stt_sigstop_for_s, "stt_sigcont"))
    events.sort(key=lambda e: (e.at_s, e.kind))
    return events


def pick_kill_targets(rng: random.Random, session_ids: Sequence[str], fraction: float) -> list[str]:
    """``ceil(n × fraction)`` distinct sessions, at least one when the fraction is positive."""
    if fraction <= 0 or not session_ids:
        return []
    count = max(1, int(-(-len(session_ids) * fraction // 1)))
    return rng.sample(list(session_ids), min(count, len(session_ids)))


def slow_viewer_indexes(scenario: Scenario, n_sessions: int) -> frozenset[int]:
    """Deterministic choice of which sessions get the slow viewer: every k-th session."""
    slow = scenario.slow_viewers(n_sessions)
    if slow <= 0:
        return frozenset()
    step = n_sessions / slow
    return frozenset(int(i * step) for i in range(slow))
