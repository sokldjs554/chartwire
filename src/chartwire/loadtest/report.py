"""Load-test report shaping (spec §11.2, §11.4) — pure functions over client bookkeeping, resource
samples and the post-run database check.

Output shapes are the ones ``scripts/readme_numbers.py::KEYS`` reads (WP-F contract):

* ``A.json``  ``{"runs": [{"n", "ack_p50_ms", "ack_p95_ms", "ack_p99_ms", "final_e2e_p95_ms",
  "alert_e2e_p95_ms", "chunks_per_s", "credit_min", "loss", "dup", …}, …]}`` (one entry per N,
  merged across invocations);
* ``B.json``  ``{credit_zero_at_s, pause_count, stream_len_max, api_rss_slope_mb_per_min, loss}``;
* ``C.json``  ``{ack_p95_ms, ack_p95_delta_pct, dropped_partials}`` (delta against ``A.json`` N=100);
* ``D.json``  ``{resume_success_pct, superseded_closes, rebuild_count, loss, dup}`` + the final invariants;
* ``docs/eval/alert_latency.json`` ``{p50_ms, p95_ms, n_sessions}`` written from scenario A.

Every report gets the §11.1 header via :func:`chartwire.eval.report.build_report`. Numbers that were
not measured are ``null`` so the matching README row is deleted rather than guessed (§0 rule 2).
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from chartwire.eval.report import build_report
from chartwire.loadtest.client import SessionStats, percentile

LOAD_CAPTION = "same host, 4 vCPU, loopback, STT simulator, client-confounded"
"""Caption every README load number carries (§11.2)."""

# --------------------------------------------------------------------------- resources


@dataclass(frozen=True)
class ResourceSample:
    t_s: float
    cpu_pct: float
    rss_mb: float


def rss_slope_mb_per_min(samples: Sequence[ResourceSample]) -> float | None:
    """Least-squares slope of RSS over time (MB per minute); ``None`` with fewer than 2 samples."""
    if len(samples) < 2:
        return None
    n = len(samples)
    mean_t = sum(s.t_s for s in samples) / n
    mean_r = sum(s.rss_mb for s in samples) / n
    var = sum((s.t_s - mean_t) ** 2 for s in samples)
    if var == 0:
        return None
    cov = sum((s.t_s - mean_t) * (s.rss_mb - mean_r) for s in samples)
    return round(cov / var * 60.0, 3)


def summarize_resources(series: Mapping[str, Sequence[ResourceSample]]) -> dict[str, dict[str, Any]]:
    """Per process: mean/max CPU %, RSS start/max/end and the RSS slope."""
    out: dict[str, dict[str, Any]] = {}
    for name, samples in series.items():
        if not samples:
            out[name] = {"samples": 0}
            continue
        cpu = [s.cpu_pct for s in samples]
        rss = [s.rss_mb for s in samples]
        out[name] = {
            "samples": len(samples),
            "cpu_pct_mean": round(sum(cpu) / len(cpu), 1),
            "cpu_pct_max": round(max(cpu), 1),
            "rss_mb_start": round(rss[0], 1),
            "rss_mb_max": round(max(rss), 1),
            "rss_mb_end": round(rss[-1], 1),
            "rss_slope_mb_per_min": rss_slope_mb_per_min(samples),
        }
    return out


# --------------------------------------------------------------------------- client aggregation


@dataclass
class DbCheck:
    """What the database says after the clients stopped (the ledger is the truth, §0 rule 3)."""

    sessions: int = 0
    chunk_rows: int = 0
    chunks_sent: int = 0
    sessions_ended: int = 0
    sessions_transcribed: int = 0
    """Sessions the stt-worker has already flushed (state ``transcribed`` or ``drafted``).
    ``stt_offsets_complete`` cannot be read without it: both are sampled **at check time**, so a
    scenario whose STT is deliberately behind (B: ``SlowStt`` 400 ms) reports a low count because the
    pipeline had not drained yet — not because the invariant is broken."""
    stt_offsets_complete: int = 0
    """Sessions whose ``stt_offsets.last_chunk_seq == sessions.final_seq`` (scenario D invariant),
    at check time — see :attr:`sessions_transcribed`."""
    segments_contiguous: int = 0
    """Sessions whose segment seqs are ``0..max`` without holes. A session that produced **no** rows
    counts as trivially contiguous, so read it next to :attr:`segments_zero_row` — otherwise a run
    that shed sessions before they sent anything reads as 100 % contiguous."""
    segments_zero_row: int = 0
    """Sessions with zero ``transcript_segments`` rows (included in :attr:`segments_contiguous`)."""
    segment_rows: int = 0
    risk_events: int = 0
    per_session_loss: dict[str, int] = field(default_factory=dict)

    @property
    def loss(self) -> int:
        return max(0, self.chunks_sent - self.chunk_rows)

    def as_dict(self) -> dict[str, Any]:
        return {
            "sessions": self.sessions,
            "chunks_sent": self.chunks_sent,
            "chunk_rows": self.chunk_rows,
            "loss": self.loss,
            "sessions_ended": self.sessions_ended,
            "sessions_transcribed": self.sessions_transcribed,
            "stt_offsets_complete": self.stt_offsets_complete,
            "segments_contiguous": self.segments_contiguous,
            "segments_zero_row": self.segments_zero_row,
            "segment_rows": self.segment_rows,
            "risk_events": self.risk_events,
            "sessions_with_loss": sum(1 for v in self.per_session_loss.values() if v > 0),
        }


def _rounded(value: float | None, digits: int = 1) -> float | None:
    return None if value is None else round(value, digits)


def aggregate(stats: Iterable[SessionStats], *, duration_s: float, ramp_s: float = 0.0) -> dict[str, Any]:
    """Pool every session's samples (§11.2 definitions) into one dict; percentiles are over the pooled
    samples, counters are sums, ``credit_min`` is the minimum over sessions."""
    rows = list(stats)
    ack = [ms for s in rows for ms in s.ack_rtt_ms]
    final = [ms for s in rows for ms in s.final_e2e_ms]
    alert = [ms for s in rows for ms in s.alert_e2e_ms]
    credits = [s.credit_min for s in rows if s.credit_min is not None]
    zero_at = [s.credit_zero_at_s for s in rows if s.credit_zero_at_s is not None]
    sent = sum(s.sent for s in rows)
    outcomes: dict[str, int] = {}
    for s in rows:
        outcomes[s.outcome] = outcomes.get(s.outcome, 0) + 1
    reconnects = sum(s.reconnects for s in rows)
    resumes_ok = sum(s.resumes_ok for s in rows)
    return {
        "sessions": len(rows),
        "duration_s": duration_s,
        "ramp_s": ramp_s,
        "chunks_sent": sent,
        "chunks_resent": sum(s.resent for s in rows),
        "chunks_per_s": _rounded(sent / duration_s if duration_s > 0 else None),
        "ack_samples": len(ack),
        "ack_p50_ms": _rounded(percentile(ack, 50)),
        "ack_p95_ms": _rounded(percentile(ack, 95)),
        "ack_p99_ms": _rounded(percentile(ack, 99)),
        "final_samples": len(final),
        "final_e2e_p50_ms": _rounded(percentile(final, 50)),
        "final_e2e_p95_ms": _rounded(percentile(final, 95)),
        "alert_samples": len(alert),
        "alert_sessions": sum(1 for s in rows if s.alert_e2e_ms),
        "alert_e2e_p50_ms": _rounded(percentile(alert, 50)),
        "alert_e2e_p95_ms": _rounded(percentile(alert, 95)),
        "credit_min": min(credits) if credits else None,
        "credit_zero_sessions": len(zero_at),
        "credit_zero_at_s": _rounded(min(zero_at), 2) if zero_at else None,
        "credit_waits": sum(s.credit_waits for s in rows),
        "pause_count": sum(s.pauses for s in rows),
        "nacks": sum(s.nacks for s in rows),
        "reconnects": reconnects,
        "resumes_ok": resumes_ok,
        "resume_success_pct": _rounded(100.0 * resumes_ok / reconnects) if reconnects else None,
        "superseded_closes": sum(s.close_codes.get(4409, 0) for s in rows),
        "loss_client": sum(s.loss for s in rows),
        "finals": sum(s.finals for s in rows),
        "final_dups": sum(s.final_dups for s in rows),
        "final_out_of_order": sum(s.final_out_of_order for s in rows),
        "partials": sum(s.partials for s in rows),
        "alerts": sum(s.alerts for s in rows),
        "lagged_dropped": sum(s.lagged_dropped for s in rows),
        "viewer_reconnects": sum(s.viewer_reconnects for s in rows),
        "outcomes": dict(sorted(outcomes.items())),
        "errors": sum(len(s.errors) for s in rows),
    }


# --------------------------------------------------------------------------- scenario bodies


def a_run(
    n: int,
    agg: Mapping[str, Any],
    resources: Mapping[str, Any],
    db: DbCheck | None,
    *,
    metrics: Mapping[str, float] | None = None,
) -> dict[str, Any]:
    """One ``runs[]`` entry of ``A.json`` (keys ``load.A.n{n}.*``)."""
    return {
        "n": n,
        "ack_p50_ms": agg["ack_p50_ms"],
        "ack_p95_ms": agg["ack_p95_ms"],
        "ack_p99_ms": agg["ack_p99_ms"],
        "final_e2e_p95_ms": agg["final_e2e_p95_ms"],
        "alert_e2e_p95_ms": agg["alert_e2e_p95_ms"],
        "chunks_per_s": agg["chunks_per_s"],
        "credit_min": agg["credit_min"],
        "loss": db.loss if db is not None else agg["loss_client"],
        "dup": agg["final_dups"],
        "clients": dict(agg),
        "db": db.as_dict() if db is not None else None,
        "resources": dict(resources),
        "metrics": dict(metrics or {}),
        "caption": LOAD_CAPTION,
    }


def merge_a_runs(existing: Mapping[str, Any] | None, run: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Replace the entry with the same ``n`` (a re-measurement wins), keep the rest, sort by ``n``."""
    runs = [
        dict(r) for r in (existing or {}).get("runs", []) if isinstance(r, dict) and r.get("n") != run["n"]
    ]
    runs.append(dict(run))
    runs.sort(key=lambda r: int(r["n"]))
    return runs


def b_body(
    agg: Mapping[str, Any],
    resources: Mapping[str, Any],
    db: DbCheck | None,
    *,
    stream_len_max: int | None,
    stream_maxlen: int,
) -> dict[str, Any]:
    api = resources.get("api", {})
    return {
        "credit_zero_at_s": agg["credit_zero_at_s"],
        "credit_zero_sessions": agg["credit_zero_sessions"],
        "pause_count": agg["pause_count"],
        "stream_len_max": stream_len_max,
        "stream_maxlen": stream_maxlen,
        "stream_bounded": None if stream_len_max is None else stream_len_max <= stream_maxlen,
        "api_rss_slope_mb_per_min": api.get("rss_slope_mb_per_min"),
        "loss": db.loss if db is not None else agg["loss_client"],
        "clients": dict(agg),
        "db": db.as_dict() if db is not None else None,
        "resources": dict(resources),
        "caption": LOAD_CAPTION,
    }


def c_body(
    agg: Mapping[str, Any],
    resources: Mapping[str, Any],
    db: DbCheck | None,
    *,
    reference_ack_p95_ms: float | None,
    dropped_partials: int | None,
    slow_viewers: int,
) -> dict[str, Any]:
    p95 = agg["ack_p95_ms"]
    delta = None
    if p95 is not None and reference_ack_p95_ms:
        delta = round(100.0 * (p95 - reference_ack_p95_ms) / reference_ack_p95_ms, 1)
    return {
        "ack_p95_ms": p95,
        "reference_ack_p95_ms": reference_ack_p95_ms,
        "ack_p95_delta_pct": delta,
        "dropped_partials": dropped_partials,
        "slow_viewers": slow_viewers,
        "finals_delivered": agg["finals"],
        "loss": db.loss if db is not None else agg["loss_client"],
        "clients": dict(agg),
        "db": db.as_dict() if db is not None else None,
        "resources": dict(resources),
        "caption": LOAD_CAPTION,
    }


def d_body(
    agg: Mapping[str, Any],
    resources: Mapping[str, Any],
    db: DbCheck | None,
    *,
    rebuild_count: int | None,
    chaos_log: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    invariants = None
    if db is not None:
        invariants = {
            "stt_offsets_complete_pct": _rounded(100.0 * db.stt_offsets_complete / db.sessions)
            if db.sessions
            else None,
            "segments_contiguous_pct": _rounded(100.0 * db.segments_contiguous / db.sessions)
            if db.sessions
            else None,
            "sessions_ended_pct": _rounded(100.0 * db.sessions_ended / db.sessions) if db.sessions else None,
        }
    return {
        "resume_success_pct": agg["resume_success_pct"],
        "reconnects": agg["reconnects"],
        "resumes_ok": agg["resumes_ok"],
        "superseded_closes": agg["superseded_closes"],
        "rebuild_count": rebuild_count,
        "loss": db.loss if db is not None else agg["loss_client"],
        "dup": agg["final_dups"],
        "invariants": invariants,
        "chaos": [dict(e) for e in chaos_log],
        "clients": dict(agg),
        "db": db.as_dict() if db is not None else None,
        "resources": dict(resources),
        "caption": LOAD_CAPTION,
    }


def alert_latency_body(agg: Mapping[str, Any], *, n_run: int) -> dict[str, Any] | None:
    """``docs/eval/alert_latency.json`` (keys ``eval.alert_latency.*``); ``None`` without samples."""
    if not agg["alert_samples"]:
        return None
    return {
        "p50_ms": agg["alert_e2e_p50_ms"],
        "p95_ms": agg["alert_e2e_p95_ms"],
        "n_sessions": agg["alert_sessions"],
        "n_alerts": agg["alert_samples"],
        "scenario": "A",
        "n_run": n_run,
        "definition": "committed_at (server) -> viewer risk.alert receipt, same host clock",
        "caption": LOAD_CAPTION,
    }


# --------------------------------------------------------------------------- files


def write_json(path: Path, report: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else None


def finish(seed: int, body: Mapping[str, Any], *, pg_version: str | None) -> dict[str, Any]:
    return build_report(seed, dict(body), pg_version=pg_version)


# --------------------------------------------------------------------------- results.md


def _fmt(value: Any, digits: int = 0) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "예" if value else "아니오"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def _res_row(name: str, res: Mapping[str, Any]) -> str:
    r = res.get(name) or {}
    return (
        f"| {name} | {_fmt(r.get('cpu_pct_mean'), 1)} | {_fmt(r.get('cpu_pct_max'), 1)} | "
        f"{_fmt(r.get('rss_mb_max'), 1)} | {_fmt(r.get('rss_slope_mb_per_min'), 3)} |"
    )


def _resources_table(res: Mapping[str, Any]) -> list[str]:
    lines = [
        "| 프로세스 | CPU 평균 % | CPU 최대 % | RSS 최대 MB | RSS 기울기 MB/min |",
        "|---|---|---|---|---|",
    ]
    lines.extend(
        _res_row(n, res) for n in ("api", "worker", "stt-worker", "postgres", "redis", "client") if n in res
    )
    return lines


def _outcomes(entry: Mapping[str, Any]) -> str:
    """``clients.outcomes`` verbatim. ``loss`` counts ledger rows against chunks *sent*, so it is
    structurally blind to a session that never sent a chunk: without this row a run where a fifth of
    the sessions never finished still reads as "loss 0, dup 0"."""
    outcomes = ((entry.get("clients") or {}).get("outcomes")) or {}
    if not outcomes:
        return "—"
    return " · ".join(f"{k} {v}" for k, v in sorted(outcomes.items(), key=lambda kv: (-kv[1], kv[0])))


def _db_table(entry: Mapping[str, Any], label: str) -> list[str]:
    """DB 대조 표. ``stt_offsets_complete`` 는 **검사 시점**의 값이므로 파이프라인이 밀린 것을 소화했는지
    (``stt_drained``)와 함께 읽어야 한다 — 그래서 같은 표에 넣는다."""
    db = entry.get("db") or {}
    ended = db.get("sessions_ended")
    attempted = db.get("sessions")
    zero_row = db.get("segments_zero_row")
    contiguous = f"| 세그먼트 seq 연속 세션 | {_fmt(db.get('segments_contiguous'))} / {_fmt(attempted)}"
    contiguous += (
        f" (행 0 세션 {_fmt(zero_row)} 포함) |"
        if zero_row is not None
        else " (행이 0 인 세션도 연속으로 센다) |"
    )
    return [
        "",
        f"DB 대조 ({label}):",
        "",
        "| 항목 | 값 |",
        "|---|---|",
        f"| 세션 (ended / transcribed / 시도) | {_fmt(ended)} / {_fmt(db.get('sessions_transcribed'))} / {_fmt(attempted)} |",
        f"| 세션 결과 (클라이언트가 본 것) | {_outcomes(entry)} |",
        f"| `stt_offsets.last_chunk_seq == final_seq` | {_fmt(db.get('stt_offsets_complete'))} / {_fmt(ended)} |",
        contiguous,
        f"| 세그먼트 행 / 위험 이벤트 | {_fmt(db.get('segment_rows'))} / {_fmt(db.get('risk_events'))} |",
        f"| loss (전송 − 원장) | {_fmt(db.get('loss'))} |",
        f"| stt 파이프라인 소화 완료 / 대기 (s) | {_fmt(entry.get('stt_drained'))} / {_fmt(entry.get('stt_drain_wait_s'), 1)} |",
        f"| 검사 시각 (녹음 시작 후 s) | {_fmt(entry.get('db_check_at_s'), 1)} |",
        f"| 이전 실행 잔여 세션 제거 | {_fmt(entry.get('stale_sessions_evicted'))} |",
        f"| 데이터베이스 | `{entry.get('database') or '—'}` |",
    ]


def _header_line(rep: Mapping[str, Any]) -> str:
    return (
        f"seed {rep.get('seed')} · git `{str(rep.get('git_sha', ''))[:12]}` · {rep.get('generated_at')} · "
        f"{rep.get('cpu')} · RAM {rep.get('ram_gb')} GB · Python {rep.get('python')} · PG {rep.get('pg_version')}"
    )


def render_results_md(reports: Mapping[str, Mapping[str, Any]]) -> str:
    """Korean ``docs/loadtest/results.md`` from the JSON reports present (missing scenarios are listed
    as 미측정). Every number below is copied from JSON produced by the runner — nothing is typed."""
    out: list[str] = [
        "# 부하 테스트 결과 (spec §11.2)",
        "",
        "> 모든 데이터는 합성(SYNTHETIC)입니다 — 실제 환자 정보 없음. 이 문서는 `chartwire loadtest <scenario>` 가",
        "> `docs/loadtest/*.json` 에 쓴 값으로 `chartwire loadtest results` 가 생성한다(손으로 적은 숫자 없음).",
        f"> 캡션: **{LOAD_CAPTION}** — api/worker/stt-worker/PG/Redis/클라이언트가 한 박스에서 돌았고 클라이언트",
        "> 시계로 잰 값이라 서버만의 지연이 아니다.",
        "",
        "정의: `ack_rtt` = 바이너리 프레임 전송 → 그 seq 를 덮는 누적 `ack` 수신(클라이언트 시계, 50 ms 그룹 커밋 창 포함) ·",
        "`final_e2e` = 발화의 마지막 청크 전송 → 뷰어 `transcript.final` 수신 · `alert_e2e` = 서버 `committed_at` → 뷰어 `risk.alert` 수신 ·",
        "`loss` = 전송 seq 수 − `audio_chunks` 행 수 · `dup` = 뷰어에 같은 세그먼트 seq 가 두 번 배달된 수(DB 행 중복은 PK 로 0) ·",
        "**`loss` 는 보낸 적 없는 청크를 셀 수 없다** — 세션이 통째로 떨어진 경우는 `세션 결과` 행(클라이언트가 본 outcome)과",
        "`세션 (ended / transcribed / 시도)` 의 분모로만 보인다. `세그먼트 seq 연속 세션` 도 행이 0 인 세션을 연속으로 세므로",
        "괄호 안의 `행 0 세션` 과 함께 읽어야 한다 ·",
        "`superseded_closes` = 클라이언트가 관찰한 4409 종료 수(close 프레임 없이 끊긴 소켓은 서버 쪽 좀비 4409 를 받지 못하므로 세지 않는다;",
        '옛 epoch 프레임의 펜싱 자체는 `ws_chunks_total{result="stale"}` 에 있다) · `rebuild_count` = stt-worker `stt_rebuilds_total`.',
        "",
    ]
    a = reports.get("A")
    out.append("## A — N 세션 × 200 ms 청크 × 뷰어 1 × 60 s")
    out.append("")
    if a and a.get("runs"):
        out.append(f"_{_header_line(a)}_")
        out.append("")
        out.append(
            "| N | chunks/s | ack p50 ms | ack p95 ms | ack p99 ms | final e2e p95 ms | alert e2e p95 ms | credit min | loss | dup |"
        )
        out.append("|---|---|---|---|---|---|---|---|---|---|")
        for run in a["runs"]:
            out.append(
                f"| {run['n']} | {_fmt(run.get('chunks_per_s'))} | {_fmt(run.get('ack_p50_ms'))} | "
                f"{_fmt(run.get('ack_p95_ms'))} | {_fmt(run.get('ack_p99_ms'))} | {_fmt(run.get('final_e2e_p95_ms'))} | "
                f"{_fmt(run.get('alert_e2e_p95_ms'))} | {_fmt(run.get('credit_min'))} | {_fmt(run.get('loss'))} | "
                f"{_fmt(run.get('dup'))} |"
            )
        for run in a["runs"]:
            out.append("")
            out.append(f"자원 (N={run['n']}):")
            out.append("")
            out.extend(_resources_table(run.get("resources") or {}))
            out.extend(_db_table(run, f"N={run['n']}"))
    else:
        out.append("미측정.")
    out.append("")

    b = reports.get("B")
    out.append("## B — SlowStt 400 ms, N=50 (credit → 0, 유한 큐, 평평한 RSS)")
    out.append("")
    if b:
        out.append(f"_{_header_line(b)}_")
        out.append("")
        out.append("| 항목 | 값 |")
        out.append("|---|---|")
        out.append(f"| credit 가 0 에 닿은 시각 (s) | {_fmt(b.get('credit_zero_at_s'), 1)} |")
        out.append(f"| credit 0 을 본 세션 수 | {_fmt(b.get('credit_zero_sessions'))} |")
        out.append(f"| `pause` 수신 수 | {_fmt(b.get('pause_count'))} |")
        out.append(
            f"| 스트림 길이 최대 (상한 {_fmt(b.get('stream_maxlen'))}) | {_fmt(b.get('stream_len_max'))} |"
        )
        out.append(f"| api RSS 기울기 (MB/min) | {_fmt(b.get('api_rss_slope_mb_per_min'), 3)} |")
        out.append(f"| loss | {_fmt(b.get('loss'))} |")
        out.append("")
        out.extend(_resources_table(b.get("resources") or {}))
        out.extend(_db_table(b, "N=50"))
    else:
        out.append("미측정.")
    out.append("")

    c = reports.get("C")
    out.append("## C — 느린 뷰어 20 %, N=100")
    out.append("")
    if c:
        out.append(f"_{_header_line(c)}_")
        out.append("")
        out.append("| 항목 | 값 |")
        out.append("|---|---|")
        out.append(f"| 녹음기 ack p95 (ms) | {_fmt(c.get('ack_p95_ms'))} |")
        out.append(f"| A N=100 의 ack p95 (ms) | {_fmt(c.get('reference_ack_p95_ms'))} |")
        out.append(f"| 변화 (%) | {_fmt(c.get('ack_p95_delta_pct'), 1)} |")
        out.append(f"| 느린 뷰어 수 | {_fmt(c.get('slow_viewers'))} |")
        out.append(f"| 폐기된 partial (`ws_dropped_partials_total`) | {_fmt(c.get('dropped_partials'))} |")
        out.append(f"| 배달된 final | {_fmt(c.get('finals_delivered'))} |")
        out.append(f"| loss | {_fmt(c.get('loss'))} |")
        out.extend(_db_table(c, "N=100"))
    else:
        out.append("미측정.")
    out.append("")

    d = reports.get("D")
    out.append("## D — 카오스, N=100 (소켓 강제 종료 10 %/10 s · Redis flush 30 s · stt-worker SIGSTOP 15 s)")
    out.append("")
    if d:
        out.append(f"_{_header_line(d)}_")
        out.append("")
        inv = d.get("invariants") or {}
        out.append("| 항목 | 값 |")
        out.append("|---|---|")
        out.append(
            f"| 재접속 시도 / resume 성공 | {_fmt(d.get('reconnects'))} / {_fmt(d.get('resumes_ok'))} |"
        )
        out.append(f"| resume 성공률 (%) | {_fmt(d.get('resume_success_pct'), 1)} |")
        out.append(f"| superseded(4409) 종료 | {_fmt(d.get('superseded_closes'))} |")
        out.append(f"| 원장 rebuild (`stt_rebuilds_total`) | {_fmt(d.get('rebuild_count'))} |")
        out.append(
            f"| `stt_offsets.last_chunk_seq == final_seq` 세션 비율 (%) | {_fmt(inv.get('stt_offsets_complete_pct'), 1)} |"
        )
        out.append(f"| 세그먼트 seq 연속 세션 비율 (%) | {_fmt(inv.get('segments_contiguous_pct'), 1)} |")
        out.append(f"| loss / dup | {_fmt(d.get('loss'))} / {_fmt(d.get('dup'))} |")
        chaos = d.get("chaos") or []
        if chaos:
            out.append("")
            out.append("카오스 이벤트 (실제 실행 시각):")
            out.append("")
            out.append("| t (s) | 종류 | 대상 |")
            out.append("|---|---|---|")
            for e in chaos:
                out.append(f"| {_fmt(e.get('at_s'), 1)} | {e.get('kind')} | {_fmt(e.get('targets'))} |")
        out.extend(_db_table(d, "N=100"))
    else:
        out.append("미측정.")
    out.append("")

    h = reports.get("H")
    out.append("## H — 아웃박스 벤치 (`chartwire outbox bench`)")
    out.append("")
    if h:
        out.append(f"_{_header_line(h)}_")
        out.append("")
        out.append("| 항목 | 값 |")
        out.append("|---|---|")
        out.append(
            f"| 이벤트 / 워커 / 테넌트 | {_fmt(h.get('events'))} / {_fmt(h.get('workers'))} / {_fmt(h.get('tenants'))} |"
        )
        out.append(f"| events/s | {_fmt(h.get('events_per_s'), 1)} |")
        out.append(f"| DLQ | {_fmt(h.get('dlq_count'))} |")
        out.append(
            f"| 워커 SIGKILL 후 reclaim | {_fmt(h.get('reclaimed'))} (killed={_fmt(h.get('worker_killed'))}) |"
        )
        out.append(
            f"| claim p50 / p95 (ms) | {_fmt(h.get('claim_ms_p50'), 1)} / {_fmt(h.get('claim_ms_p95'), 1)} |"
        )
    else:
        out.append("미측정.")
    out.append("")
    out.append("## E — drain (선택)")
    out.append("")
    e = reports.get("E")
    if e:
        out.append(f"loss {_fmt(e.get('loss'))} · reconnect p95 {_fmt(e.get('reconnect_p95_ms'))} ms")
    else:
        out.append("미실행 (스펙 §11.2: 시간이 남을 때만).")
    out.append("")
    return "\n".join(out)


def load_reports(out_dir: Path) -> dict[str, dict[str, Any]]:
    reports: dict[str, dict[str, Any]] = {}
    for name in ("A", "B", "C", "D", "E", "H"):
        data = read_json(out_dir / f"{name}.json")
        if data is not None:
            reports[name] = data
    return reports
