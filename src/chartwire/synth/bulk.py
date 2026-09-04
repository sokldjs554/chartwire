"""``chartwire synth bulk`` — the perf-study dataset (spec §4.6, §10.3 ``bulk``).

Default shape (``--segments 2000000 --tenants 8``): 20,000 sessions × 100 segments spread over the
last 24 monthly partitions, 50,000 patients, ``segment_search`` for 60 % of the sessions with
controlled keyword rates (``불면`` 2 % including ``불면증`` 0.5 %, ``에스시탈로프람`` 0.3 %, ``자해``
0.2 %, ``terms[]`` from the lexicon tagger), ``risk_events`` for 2 % of segments (1 % of them
unacknowledged) and one outbox row per segment (99.9 % ``done``). Everything scales linearly with
``--segments`` (``--segments 20000`` is the integration test).

Loading goes through asyncpg ``copy_records_to_table`` as ``chartwire_owner``. FORCE RLS applies to
the owner too, and PostgreSQL refuses ``COPY FROM`` outright on a table whose policies apply to the
current role (``FeatureNotSupportedError: COPY FROM not supported with row-level security``), so
every batch is COPYed into a temporary staging table and moved with ``INSERT … SELECT`` inside one
transaction per tenant with ``app.tenant_id`` set — the rows pass the same ``WITH CHECK`` policy the
app's inserts pass; the loader is not a bypass. ``text_enc`` is the one-byte
placeholder ``\\x00`` (bulk rows are never decrypted; documented in ``docs/perf/README.md``), patient
identifiers are placeholders too, only ``name_hmac`` is a real blind index so Q6 has something to
look up. The generator is a pure function of ``--seed``; a second run with the same seed refuses
to load (tenant slugs collide) rather than doubling the data.
"""

from __future__ import annotations

import json
import random
import time
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from uuid import UUID

import asyncpg
from sqlalchemy.engine import make_url

from chartwire.auth import passwords
from chartwire.crypto.blind_index import blind_index
from chartwire.crypto.envelope import Envelope, dek_fingerprint
from chartwire.crypto.kek import LocalKek
from chartwire.db.cli import month_start
from chartwire.risk.terms import lexicon_tag
from chartwire.synth import grammar
from chartwire.synth import vocab_ko as v

SEGMENTS_PER_SESSION: Final = 100
PATIENTS_PER_SESSION: Final = 2.5  # 50,000 patients / 20,000 sessions
MONTHS: Final = 24
SEARCH_SESSION_P: Final = 0.60
RISK_P: Final = 0.02
RISK_OPEN_P: Final = 0.01
OUTBOX_PENDING_P: Final = 0.001
TEXT_ENC_PLACEHOLDER: Final = b"\x00"
PLACEHOLDER_DEK: Final = bytes(60)
MS_PER_CHAR: Final = 180
PAUSE_MS: Final = (400, 900)
COPY_BATCH_SESSIONS: Final = 200  # 20,000 segments per COPY call
SLA_S: Final = {3: 60, 2: 300}  # risk/alerts.sla_deadline: level 3 → 60 s, level 2 → 300 s, level 1 → none

KEYWORD_RATES: Final[tuple[tuple[str, float], ...]] = (
    ("불면증", 0.005),
    ("불면", 0.015),  # + 0.005 from 불면증 (substring) = 2 % of rows contain 불면
    ("에스시탈로프람", 0.003),
    ("자해", 0.002),
)
KEYWORD_SENTENCES: Final[dict[str, tuple[tuple[str, str], ...]]] = {
    "불면증": (
        ("patient", "불면증 때문에 왔어요"),
        ("patient", "예전에 불면증으로 약을 먹은 적이 있어요"),
        ("clinician", "불면증이 다시 심해지신 거군요"),
    ),
    "불면": (
        ("patient", "요즘 불면 때문에 낮에 너무 힘들어요"),
        ("patient", "불면이 심해져서 수면제를 먹고 있어요"),
        ("patient", "불면 증상이 3주째 계속돼요"),
        ("clinician", "불면은 지난번보다 나아지셨어요?"),
    ),
    "에스시탈로프람": (
        ("patient", "에스시탈로프람 10mg 먹고 있어요"),
        ("clinician", "에스시탈로프람을 15mg으로 올려보겠습니다"),
        ("patient", "에스시탈로프람 먹고 나서 속이 좀 울렁거려요"),
    ),
    "자해": (
        ("patient", "자해 같은 건 전혀 안 해요"),
        ("patient", "자해 충동은 요즘 없어요"),
        ("clinician", "혹시 자해를 하신 적이 있으세요?"),
    ),
}
RISK_PHRASES: Final[dict[str, str]] = {
    "suicidal_ideation": "죽고 싶",
    "self_harm": "손목을 긋",
    "harm_to_others": "죽이고 싶",
    "substance_acute": "술 마시고 약을",
}


class BulkAlreadyLoaded(RuntimeError):
    """Tenant ``bulk-<seed>-01`` exists: the dataset for this seed is already in the database."""


@dataclass(frozen=True)
class BulkPlan:
    seed: int
    tenants: int
    segments: int
    sessions: int
    patients: int
    months: int = MONTHS

    @classmethod
    def build(cls, *, seed: int, segments: int, tenants: int = 8, months: int = MONTHS) -> BulkPlan:
        if segments < SEGMENTS_PER_SESSION * tenants:
            raise ValueError(
                f"--segments 는 최소 {SEGMENTS_PER_SESSION * tenants} (테넌트당 세션 1개) 이어야 합니다"
            )
        sessions = segments // SEGMENTS_PER_SESSION
        return cls(
            seed=seed,
            tenants=tenants,
            segments=sessions * SEGMENTS_PER_SESSION,
            sessions=sessions,
            patients=max(tenants, int(sessions * PATIENTS_PER_SESSION)),
            months=months,
        )

    def slug(self, k: int) -> str:
        return f"bulk-{self.seed}-{k:02d}"

    def per_tenant(self, total: int, k: int) -> int:
        """Split ``total`` over the tenants; the remainder goes to the first ones."""
        base, extra = divmod(total, self.tenants)
        return base + (1 if k <= extra else 0)


@dataclass
class BulkReport:
    plan: BulkPlan
    tenant_ids: list[str] = field(default_factory=list)
    partitions: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    timings_s: dict[str, float] = field(default_factory=dict)
    total_s: float = 0.0
    segment_id_range: tuple[int, int] = (0, 0)

    def as_dict(self) -> dict[str, Any]:
        return {
            "plan": asdict(self.plan),
            "tenant_ids": self.tenant_ids,
            "partitions": self.partitions,
            "counts": self.counts,
            "timings_s": {k: round(t, 3) for k, t in self.timings_s.items()},
            "total_s": round(self.total_s, 3),
            "segment_id_range": list(self.segment_id_range),
        }


# ------------------------------------------------------------------ sentence pool


@dataclass(frozen=True)
class Sentence:
    speaker: str
    text: str
    terms: tuple[str, ...]


def _controlled(text: str) -> bool:
    return any(k in text for k, _ in KEYWORD_RATES)


def build_pool(seed: int, draws: int = 4000) -> tuple[list[Sentence], dict[str, list[Sentence]]]:
    """Every distinct grammar sentence reachable in ``draws`` renderings that is free of the controlled
    keywords (≈140 with the §10.1 vocabulary), plus one list per controlled keyword. ``terms`` are
    tagged once per distinct sentence — the tagger is the expensive part of a 1.2 M-row index."""
    rng = random.Random(f"bulk-pool:{seed}")
    templates = [
        (t, "patient")
        for t in v.SLEEP
        + v.APPETITE
        + v.MOOD
        + v.ANXIETY
        + v.CONCENTRATION
        + v.MEDICATION
        + v.ALCOHOL
        + v.DURATION
    ] + [(t, "clinician") for t in v.PLANS + v.OBSERVATIONS]
    fixed = [("clinician", q) for qs in v.QUESTIONS.values() for q in qs] + [
        ("patient", a) for a in v.NEUTRAL_RISK_ANSWERS
    ]
    candidates: list[tuple[str, str]] = list(fixed)
    for _ in range(draws):
        template, speaker = rng.choice(templates)
        candidates.append((speaker, grammar.render(template, rng)[0]))
    seen: set[str] = set()
    base: list[Sentence] = []
    for speaker, text in candidates:
        if _controlled(text) or text in seen:
            continue
        seen.add(text)
        base.append(Sentence(speaker, text, tuple(lexicon_tag(text))))
    keyed = {
        k: [Sentence(sp, txt, tuple(lexicon_tag(txt))) for sp, txt in sents]
        for k, sents in KEYWORD_SENTENCES.items()
    }
    return base, keyed


def pick_sentence(rng: random.Random, base: list[Sentence], keyed: dict[str, list[Sentence]]) -> Sentence:
    u = rng.random()
    threshold = 0.0
    for keyword, rate in KEYWORD_RATES:
        threshold += rate
        if u < threshold:
            return rng.choice(keyed[keyword])
    return rng.choice(base)


# ------------------------------------------------------------------ row generators


def _uuid(rng: random.Random) -> UUID:
    return UUID(int=rng.getrandbits(128), version=4)


def _dsn(owner_url: str) -> str:
    return make_url(owner_url).set(drivername="postgresql").render_as_string(hide_password=False)


@dataclass
class TenantData:
    tenant_id: UUID
    clinician_id: UUID
    patient_ids: list[UUID]
    sessions: list[tuple[UUID, UUID, datetime, bool]]
    """(session_id, patient_id, started_at, searchable)"""


def _segments_for_session(
    rng: random.Random,
    started_at: datetime,
    base: list[Sentence],
    keyed: dict[str, list[Sentence]],
) -> Iterator[tuple[int, Sentence, datetime, int, int]]:
    """Yield ``(seq, sentence, created_at, t_start_ms, t_end_ms)`` for one session."""
    t = 0
    for seq in range(SEGMENTS_PER_SESSION):
        sentence = pick_sentence(rng, base, keyed)
        t_end = t + MS_PER_CHAR * len(sentence.text) + rng.randint(*PAUSE_MS)
        yield seq, sentence, started_at + timedelta(milliseconds=t), t, t_end
        t = t_end


async def copy_via_stage(
    conn: asyncpg.Connection, table: str, columns: list[str], records: list[tuple[Any, ...]]
) -> None:
    """COPY into a temp staging table (typed like ``table``, no constraints), then ``INSERT … SELECT``
    into the RLS-protected target so the ``WITH CHECK`` policy runs. The stage is dropped at commit."""
    if not records:
        return
    stage = f"stage_{table}"
    cols = ", ".join(columns)
    await conn.execute(
        f"CREATE TEMP TABLE IF NOT EXISTS {stage} ON COMMIT DROP AS SELECT {cols} FROM {table} WITH NO DATA"
    )
    await conn.copy_records_to_table(stage, records=records, columns=columns)
    await conn.execute(f"INSERT INTO {table} ({cols}) SELECT {cols} FROM {stage}")
    await conn.execute(f"TRUNCATE {stage}")


# ------------------------------------------------------------------ loader


Log = Callable[[str], None]


async def load(
    owner_url: str,
    plan: BulkPlan,
    *,
    kek_master: bytes,
    log: Log = print,
    vacuum: bool = True,
    now: datetime | None = None,
) -> BulkReport:
    """Load the dataset described by ``plan``. Raises :class:`BulkAlreadyLoaded` for a repeated seed."""
    report = BulkReport(plan=plan)
    t_all = time.perf_counter()
    conn = await asyncpg.connect(_dsn(owner_url))
    try:
        if await conn.fetchval("SELECT 1 FROM tenants WHERE slug = $1", plan.slug(1)):
            raise BulkAlreadyLoaded(f"{plan.slug(1)} 가 이미 있습니다 — 같은 시드는 두 번 적재하지 않습니다")
        now = now or datetime.now(tz=UTC)
        with _phase(report, "partitions", log):
            report.partitions = await _ensure_partitions(conn, plan.months, now)
        with _phase(report, "pool", log):
            base, keyed = build_pool(plan.seed)
        with _phase(report, "tenants", log):
            kek = LocalKek(kek_master)
            tenants = await _create_tenants(conn, plan, kek)
            report.tenant_ids = [str(t) for t in tenants]
        first_id = int(await conn.fetchval("SELECT nextval('transcript_segments_id_seq')"))
        next_id = first_id
        outbox_n = 0
        counts: dict[str, int] = {
            "patients": 0,
            "sessions": 0,
            "segments": 0,
            "search": 0,
            "risk": 0,
            "risk_open": 0,
            "outbox": 0,
            "outbox_pending": 0,
        }
        for k, tenant_id in enumerate(tenants, start=1):
            with _phase(report, f"tenant_{k:02d}", log):
                rng = random.Random(f"bulk:{plan.seed}:{k}")
                async with conn.transaction():
                    await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))
                    data = await _load_identities(conn, rng, plan, k, tenant_id, kek_master, now)
                    counts["patients"] += len(data.patient_ids)
                    counts["sessions"] += len(data.sessions)
                    next_id, outbox_n = await _load_sessions(
                        conn, rng, plan, data, base, keyed, next_id, outbox_n, counts, now
                    )
        report.segment_id_range = (first_id, next_id - 1)
        await conn.execute("SELECT setval('transcript_segments_id_seq', $1)", max(next_id - 1, first_id))
        report.counts = counts
        if vacuum:
            with _phase(report, "vacuum_analyze", log):
                for table in (
                    "patients",
                    "sessions",
                    "transcript_segments",
                    "segment_search",
                    "risk_events",
                    "outbox_events",
                ):
                    await conn.execute(f"VACUUM ANALYZE {table}")
    finally:
        await conn.close()
    report.total_s = time.perf_counter() - t_all
    log(f"total {report.total_s:.1f}s — " + ", ".join(f"{k}={n:,}" for k, n in report.counts.items()))
    return report


class _phase:
    def __init__(self, report: BulkReport, name: str, log: Log) -> None:
        self.report, self.name, self.log = report, name, log

    def __enter__(self) -> None:
        self.t0 = time.perf_counter()

    def __exit__(self, *exc: object) -> None:
        elapsed = time.perf_counter() - self.t0
        self.report.timings_s[self.name] = elapsed
        self.log(f"{self.name:<16} {elapsed:8.2f}s")


async def _ensure_partitions(conn: asyncpg.Connection, months: int, now: datetime) -> list[str]:
    """Months ``-(months-1) .. +1`` relative to ``now`` (a session started late in the current month
    spills a few minutes into the next one)."""
    names = []
    for offset in range(-(months - 1), 2):
        names.append(
            await conn.fetchval("SELECT ensure_segment_partition($1::date)", month_start(offset, now.date()))
        )
    return names


async def _create_tenants(conn: asyncpg.Connection, plan: BulkPlan, kek: LocalKek) -> list[UUID]:
    rng = random.Random(f"bulk-tenants:{plan.seed}")
    ids = []
    for k in range(1, plan.tenants + 1):
        tenant_id = _uuid(rng)
        kek_ref = f"local:{plan.slug(k)}"
        await conn.execute(
            "INSERT INTO tenants (id, slug, name, kek_ref, record_key_wrapped, settings) VALUES ($1, $2, $3, $4, $5, $6::jsonb)",
            tenant_id,
            plan.slug(k),
            f"가상의원-bulk-{k:02d}",
            kek_ref,
            kek.wrap(Envelope.new_dek(), kek_ref),
            json.dumps({"synthetic": True, "bulk_seed": plan.seed}),
        )
        ids.append(tenant_id)
    return ids


async def _load_identities(
    conn: asyncpg.Connection,
    rng: random.Random,
    plan: BulkPlan,
    k: int,
    tenant_id: UUID,
    master: bytes,
    now: datetime,
) -> TenantData:
    clinician_id = _uuid(rng)
    email = f"clinician@{plan.slug(k)}.clinic"
    await conn.execute(
        "INSERT INTO users (id, tenant_id, role, email_hmac, email_enc, display_name, password_hash) VALUES ($1, $2, 'clinician', $3, $4, $5, $6)",
        clinician_id,
        tenant_id,
        blind_index(master, tenant_id, email),
        TEXT_ENC_PLACEHOLDER,
        f"가상임상의-bulk-{k:02d}",
        passwords.hash(f"bulk-{plan.seed}"),
    )
    fingerprint = dek_fingerprint(PLACEHOLDER_DEK)
    n_patients = plan.per_tenant(plan.patients, k)
    patient_ids = [_uuid(rng) for _ in range(n_patients)]
    span_start = datetime.combine(
        month_start(-(plan.months - 1), now.date()), datetime.min.time(), tzinfo=UTC
    )
    span_s = (now - timedelta(hours=1) - span_start).total_seconds()
    patient_rows = [
        (
            pid,
            tenant_id,
            v.pseudonym(i),
            TEXT_ENC_PLACEHOLDER,
            blind_index(master, tenant_id, v.synth_name(rng)),
            rng.randint(1958, 2006),
            rng.choice(("F", "M")),
            PLACEHOLDER_DEK,
            fingerprint,
            "granted",
            True,
            span_start + timedelta(seconds=rng.uniform(0, span_s)),
        )
        for i, pid in enumerate(patient_ids, start=1)
    ]
    await copy_via_stage(
        conn,
        "patients",
        [
            "id",
            "tenant_id",
            "pseudonym",
            "name_enc",
            "name_hmac",
            "birth_year",
            "sex",
            "dek_wrapped",
            "dek_fingerprint",
            "consent_state",
            "is_synthetic",
            "created_at",
        ],
        patient_rows,
    )
    n_sessions = plan.per_tenant(plan.sessions, k)
    sessions = [
        (
            _uuid(rng),
            rng.choice(patient_ids),
            span_start + timedelta(seconds=rng.uniform(0, span_s)),
            rng.random() < SEARCH_SESSION_P,
        )
        for _ in range(n_sessions)
    ]
    sessions.sort(key=lambda s: s[2])
    all_scopes = ["recording", "transcription", "ai_drafting", "search_index"]
    session_rows = [
        (
            sid,
            tenant_id,
            pid,
            clinician_id,
            "transcribed",
            "simulator",
            all_scopes if searchable else all_scopes[:3],
            PLACEHOLDER_DEK,
            fingerprint,
            SEGMENTS_PER_SESSION * 25,
            SEGMENTS_PER_SESSION * 25,
            started_at,
            started_at + timedelta(minutes=9),
            started_at + timedelta(minutes=10),
            started_at,
            started_at,
        )
        for sid, pid, started_at, searchable in sessions
    ]
    await copy_via_stage(
        conn,
        "sessions",
        [
            "id",
            "tenant_id",
            "patient_id",
            "clinician_id",
            "state",
            "stt_provider",
            "scopes_snapshot",
            "dek_wrapped",
            "dek_fingerprint",
            "ack_seq",
            "final_seq",
            "started_at",
            "ended_at",
            "transcribed_at",
            "created_at",
            "updated_at",
        ],
        session_rows,
    )
    return TenantData(tenant_id, clinician_id, patient_ids, sessions)


async def _load_sessions(
    conn: asyncpg.Connection,
    rng: random.Random,
    plan: BulkPlan,
    data: TenantData,
    base: list[Sentence],
    keyed: dict[str, list[Sentence]],
    next_id: int,
    outbox_n: int,
    counts: dict[str, int],
    now: datetime,
) -> tuple[int, int]:
    tenant_id = data.tenant_id
    for start in range(0, len(data.sessions), COPY_BATCH_SESSIONS):
        segments: list[tuple[Any, ...]] = []
        search: list[tuple[Any, ...]] = []
        risk: list[tuple[Any, ...]] = []
        outbox: list[tuple[Any, ...]] = []
        for sid, pid, started_at, searchable in data.sessions[start : start + COPY_BATCH_SESSIONS]:
            for seq, sentence, created_at, t_start, t_end in _segments_for_session(
                rng, started_at, base, keyed
            ):
                seg_id = next_id
                next_id += 1
                segments.append(
                    (
                        seg_id,
                        tenant_id,
                        sid,
                        pid,
                        seq,
                        sentence.speaker,
                        t_start,
                        t_end,
                        TEXT_ENC_PLACEHOLDER,
                        len(sentence.text),
                        0.9,
                        "bulk",
                        created_at,
                    )
                )
                if searchable:
                    search.append(
                        (
                            seg_id,
                            created_at,
                            tenant_id,
                            sid,
                            pid,
                            sentence.speaker,
                            sentence.text,
                            list(sentence.terms),
                            created_at,
                        )
                    )
                if rng.random() < RISK_P:
                    category = rng.choice(sorted(RISK_PHRASES))
                    severity = rng.choice((1, 2, 3))
                    detected_at = created_at + timedelta(seconds=1)
                    sla = detected_at + timedelta(seconds=SLA_S[severity]) if severity in SLA_S else None
                    open_alert = rng.random() < RISK_OPEN_P
                    acked_at = None if open_alert else detected_at + timedelta(seconds=rng.randint(5, 600))
                    risk.append(
                        (
                            tenant_id,
                            sid,
                            pid,
                            seg_id,
                            created_at,
                            seq,
                            category,
                            severity,
                            RISK_PHRASES[category],
                            0,
                            len(RISK_PHRASES[category]),
                            "{}",
                            "lex-1",
                            detected_at,
                            sla,
                            acked_at,
                            None if open_alert else data.clinician_id,
                            0,
                        )
                    )
                    counts["risk_open"] += open_alert
                outbox_n += 1
                pending = rng.random() < OUTBOX_PENDING_P
                outbox.append(
                    (
                        tenant_id,
                        "session",
                        sid,
                        "noop",
                        json.dumps({"session_id": str(sid), "seq": seq}),
                        f"bulk:{plan.seed}:{outbox_n}",
                        "pending" if pending else "done",
                        0 if pending else 1,
                        created_at,
                        created_at,
                        None if pending else created_at + timedelta(milliseconds=50),
                    )
                )
                counts["outbox_pending"] += pending
        await copy_via_stage(
            conn,
            "transcript_segments",
            [
                "id",
                "tenant_id",
                "session_id",
                "patient_id",
                "seq",
                "speaker",
                "t_start_ms",
                "t_end_ms",
                "text_enc",
                "text_len",
                "confidence",
                "provider",
                "created_at",
            ],
            segments,
        )
        await copy_via_stage(
            conn,
            "segment_search",
            [
                "segment_id",
                "segment_created_at",
                "tenant_id",
                "session_id",
                "patient_id",
                "speaker",
                "text",
                "terms",
                "created_at",
            ],
            search,
        )
        await copy_via_stage(
            conn,
            "risk_events",
            [
                "tenant_id",
                "session_id",
                "patient_id",
                "segment_id",
                "segment_created_at",
                "segment_seq",
                "category",
                "severity",
                "phrase",
                "span_start",
                "span_end",
                "scope",
                "detector_version",
                "detected_at",
                "sla_deadline_at",
                "acknowledged_at",
                "acknowledged_by",
                "escalation_level",
            ],
            risk,
        )
        await copy_via_stage(
            conn,
            "outbox_events",
            [
                "tenant_id",
                "aggregate_type",
                "aggregate_id",
                "event_type",
                "payload",
                "idempotency_key",
                "status",
                "attempts",
                "next_attempt_at",
                "created_at",
                "done_at",
            ],
            outbox,
        )
        counts["segments"] += len(segments)
        counts["search"] += len(search)
        counts["risk"] += len(risk)
        counts["outbox"] += len(outbox)
    return next_id, outbox_n
