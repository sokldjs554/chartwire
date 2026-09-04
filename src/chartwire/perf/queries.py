"""The query catalogue of the performance study (spec §4.6, Q1–Q6).

Each :class:`PerfQuery` is the SQL *as the application executes it* (same predicates as the
repository functions), the role it runs under and the parameter names it needs. ``perf.study``
binds the parameters from the bulk dataset, switches role with ``SET LOCAL ROLE`` on a superuser
connection and records ``EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)``.

Variants share a ``group`` (``Q1``, ``Q2`` …) so ``--only q1,q2`` selects whole rows of the
§4.6 table. ``Q5`` measures RLS overhead by running Q1/Q3 as ``chartwire_app`` (policy applied)
and as superuser (policy bypassed); the policy-form comparison is emulated as superuser with the
policy predicate written into the query in both shapes (inline ``NULLIF(current_setting(...))``
versus the ``(SELECT …)`` InitPlan form) — the same qual the planner would attach from the policy.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

Role = Literal["app", "owner", "superuser"]

APP_ROLE: Final = "chartwire_app"
OWNER_ROLE: Final = "chartwire_owner"
POLICY_INLINE: Final = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"
POLICY_INITPLAN: Final = "tenant_id = (SELECT NULLIF(current_setting('app.tenant_id', true), '')::uuid)"
LEAKPROOF_FUNCTIONS: Final[tuple[str, ...]] = (
    "texteq",
    "uuid_eq",
    "texticlike",
    "textlike",
    "textregexeq",
    "similarity",
    "word_similarity",
    "arraycontains",
    "ts_match_vq",
)


@dataclass(frozen=True)
class PerfQuery:
    id: str
    group: str
    label: str
    role: Role
    sql: str
    explain: bool = True
    """``False`` = wall time only (a plpgsql function call shows nothing useful in EXPLAIN)."""
    demonstrates: str = ""
    requires_after: bool = False
    """Needs an object created by revision 0007 (``search_segments``); skipped at ``before``."""


_Q1_BASE = (
    "SELECT id, seq, speaker, t_start_ms, t_end_ms, text_len, created_at FROM transcript_segments "
    "WHERE tenant_id = %(tenant_id)s AND patient_id = %(patient_id)s "
    "AND created_at >= now() - interval '6 months' "
)
_Q1_ORDER = "ORDER BY created_at DESC, id DESC LIMIT %(limit)s"
_Q1A = (
    "SELECT id, seq, speaker, t_start_ms, t_end_ms, text_len, created_at FROM transcript_segments "
    "WHERE session_id = %(session_id)s AND seq > %(after_seq)s "
)
_Q2 = (
    "SELECT segment_id, session_id, patient_id, speaker, left(text, 120), segment_created_at "
    "FROM segment_search WHERE tenant_id = %(tenant_id)s AND text ILIKE %(pattern)s "
    "ORDER BY segment_created_at DESC LIMIT 50"
)
_Q3 = (
    "SELECT id, session_id, severity, sla_deadline_at FROM risk_events "
    "WHERE tenant_id = %(tenant_id)s AND acknowledged_at IS NULL ORDER BY sla_deadline_at LIMIT 100"
)
_Q4 = (
    "SELECT id FROM outbox_events WHERE status = 'pending' AND next_attempt_at <= now() "
    "ORDER BY next_attempt_at, id LIMIT 100 FOR UPDATE SKIP LOCKED"
)
_Q6 = "SELECT id, pseudonym FROM patients WHERE tenant_id = %(tenant_id)s AND name_hmac = %(name_hmac)s"


def _q1_with_policy(policy: str) -> str:
    """Q1 as superuser with the policy qual written explicitly (RLS bypassed, qual emulated)."""
    return _Q1_BASE + f"AND {policy} " + _Q1_ORDER


QUERIES: Final[tuple[PerfQuery, ...]] = (
    PerfQuery(
        "Q1",
        "Q1",
        "환자 종단 타임라인 6개월 LIMIT 50",
        "app",
        _Q1_BASE + _Q1_ORDER,
        demonstrates="파티션 프루닝 + 무중단 파티션 인덱스 ix_segments_patient_time",
    ),
    PerfQuery(
        "Q1a_without",
        "Q1a",
        "세션 replay, created_at 조건 없음",
        "app",
        _Q1A + "ORDER BY seq LIMIT 500",
        demonstrates="파티션 키가 조건에 없으면 24개 파티션 전부 탐색",
    ),
    PerfQuery(
        "Q1a_with",
        "Q1a",
        "세션 replay, created_at >= started_at",
        "app",
        _Q1A + "AND created_at >= %(started_at)s ORDER BY seq LIMIT 500",
        demonstrates="파티션 키 조건 → 파티션 1개 (repo.segments.replay 가 sessions.started_at 을 넘김)",
    ),
    PerfQuery(
        "Q1a_bounded",
        "Q1a",
        "세션 replay, started_at ≤ created_at < started_at + 1 day",
        "app",
        _Q1A + "AND created_at >= %(started_at)s AND created_at < %(started_at)s + interval '1 day' "
        "ORDER BY seq LIMIT 500",
        demonstrates="하한만으로는 과거 파티션만 잘린다 — 상한까지 주면 파티션 1개 (repo 요청, wp-f-phase1.md)",
    ),
    PerfQuery(
        "Q1b_keyset",
        "Q1b",
        "타임라인 keyset 100번째 페이지",
        "app",
        _Q1_BASE + "AND (created_at, id) < (%(cursor_at)s, %(cursor_id)s) " + _Q1_ORDER,
        demonstrates="keyset 페이지네이션은 위치와 무관하게 인덱스 범위 탐색",
    ),
    PerfQuery(
        "Q1b_offset",
        "Q1b",
        "타임라인 OFFSET 5000",
        "app",
        _Q1_BASE + "ORDER BY created_at DESC, id DESC LIMIT %(limit)s OFFSET 5000",
        demonstrates="OFFSET 은 건너뛰는 행을 전부 읽는다",
    ),
    PerfQuery(
        "Q2a",
        "Q2",
        "ILIKE '%불면%' (2음절) as app",
        "app",
        _Q2,
        demonstrates="2음절 패턴은 트라이그램이 없어 GIN 이 있어도 Seq Scan — 정직한 한계",
    ),
    PerfQuery(
        "Q2b",
        "Q2",
        "ILIKE '%불면증%' as app (RLS)",
        "app",
        _Q2,
        demonstrates="RLS 아래에서는 texticlike 가 leakproof 가 아니라 GIN 미사용",
    ),
    PerfQuery(
        "Q2c",
        "Q2",
        "ILIKE '%불면증%' as owner (RLS 면제)",
        "owner",
        _Q2,
        demonstrates="SECURITY DEFINER 실행 컨텍스트: Bitmap Index Scan on ix_search_text_trgm",
    ),
    PerfQuery(
        "Q2d_text",
        "Q2",
        "search_segments('불면증','text') as app",
        "app",
        "SELECT * FROM search_segments('불면증', 'text')",
        explain=False,
        requires_after=True,
        demonstrates="app 역할에서의 end-to-end 수정 (wall time only)",
    ),
    PerfQuery(
        "Q2d_term",
        "Q2",
        "search_segments('불면','term') as app",
        "app",
        "SELECT * FROM search_segments('불면', 'term')",
        explain=False,
        requires_after=True,
        demonstrates="terms @> ARRAY[..] 는 배열 GIN ix_search_terms (자유 텍스트 없음)",
    ),
    PerfQuery(
        "Q3",
        "Q3",
        "미확인 경보 SLA 순 LIMIT 100",
        "app",
        _Q3,
        demonstrates="부분 인덱스 ix_risk_open_sla (대시보드가 초당 폴링)",
    ),
    PerfQuery(
        "Q4",
        "Q4",
        "outbox 클레임 FOR UPDATE SKIP LOCKED",
        "app",
        _Q4,
        demonstrates="부분 인덱스 ix_outbox_pending + bloat 관찰 (pgstattuple)",
    ),
    PerfQuery(
        "Q5_q1_app",
        "Q5",
        "Q1 as app (RLS 적용)",
        "app",
        _Q1_BASE + _Q1_ORDER,
        demonstrates="RLS 오버헤드 기준",
    ),
    PerfQuery(
        "Q5_q1_su",
        "Q5",
        "Q1 as superuser (RLS 우회)",
        "superuser",
        _Q1_BASE + _Q1_ORDER,
        demonstrates="RLS 오버헤드 비교",
    ),
    PerfQuery("Q5_q3_app", "Q5", "Q3 as app (RLS 적용)", "app", _Q3, demonstrates="RLS 오버헤드 기준"),
    PerfQuery(
        "Q5_q3_su", "Q5", "Q3 as superuser (RLS 우회)", "superuser", _Q3, demonstrates="RLS 오버헤드 비교"
    ),
    PerfQuery(
        "Q5_form_inline",
        "Q5",
        "정책 형태 NULLIF(current_setting()) 인라인",
        "superuser",
        _q1_with_policy(POLICY_INLINE),
        demonstrates="정책 qual 인라인 형태 (superuser, GUC 설정)",
    ),
    PerfQuery(
        "Q5_form_initplan",
        "Q5",
        "정책 형태 (SELECT NULLIF(...)) InitPlan",
        "superuser",
        _q1_with_policy(POLICY_INITPLAN),
        demonstrates="정책 qual InitPlan 형태 — 플랜 모양 비교",
    ),
    PerfQuery(
        "Q6",
        "Q6",
        "환자 이름 블라인드 인덱스 name_hmac",
        "app",
        _Q6,
        demonstrates="암호화 컬럼의 HMAC 정확 일치 검색 ix_patients_name_hmac",
    ),
)

PATTERN_BY_ID: Final[dict[str, str]] = {"Q2a": "%불면%", "Q2b": "%불면증%", "Q2c": "%불면증%"}


def select(only: str | None) -> list[PerfQuery]:
    """``--only q1,q2`` → queries whose group matches one of the (case-insensitive) prefixes."""
    if not only:
        return list(QUERIES)
    wanted = {token.strip().lower() for token in only.split(",") if token.strip()}
    return [q for q in QUERIES if q.group.lower() in wanted or q.id.lower() in wanted]
