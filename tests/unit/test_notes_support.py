"""Hand-built ``SegmentView`` / draft helpers shared by the ``test_notes_*`` modules (no DB)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from chartwire.notes.schema import (
    DraftContext,
    Evidence,
    NoteDraftOut,
    Section,
    SegmentView,
    Speaker,
    Statement,
    StatementKind,
)

SESSION_ID = UUID("018f0000-0000-7000-8000-000000000001")
_T0 = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)
KIND: dict[Section, StatementKind] = {"S": "reported", "O": "observed", "P": "plan_item"}


def seg(seq: int, speaker: Speaker, text: str) -> SegmentView:
    return SegmentView(
        segment_id=seq,
        seq=seq,
        speaker=speaker,
        text=text,
        t_start_ms=seq * 2000,
        t_end_ms=seq * 2000 + 1500,
        created_at=_T0,
        confidence=0.9,
    )


def session(*utterances: tuple[Speaker, str]) -> list[SegmentView]:
    return [seg(i + 1, sp, text) for i, (sp, text) in enumerate(utterances)]


def statement(section: Section, text: str, *evidence: tuple[int, str]) -> Statement:
    return Statement(
        section=section,
        text=text,
        evidence=[Evidence(seq=s, quote=q) for s, q in evidence],
        kind=KIND[section],
    )


def draft(*statements: Statement, abstain: bool = False) -> NoteDraftOut:
    return NoteDraftOut(statements=list(statements), abstain=abstain)


def context(segments: list[SegmentView], max_per_section: int = 12) -> DraftContext:
    return DraftContext(session_id=SESSION_ID, segments=segments, max_per_section=max_per_section)


# A small synthetic consultation used by several modules. All names are synthetic; no PHI.
CONSULTATION: list[SegmentView] = session(
    ("clinician", "안녕하세요, 지난 2주 동안 어떻게 지내셨어요?"),
    ("patient", "잠드는 데 두 시간쯤 걸려요"),
    ("patient", "새벽 4시에 깨서 다시 못 자요"),
    ("patient", "입맛이 없어요"),
    ("clinician", "체중 변화는 있으셨어요?"),
    ("patient", "3주 동안 3kg 빠졌어요"),
    ("patient", "기분이 계속 가라앉아요"),
    ("clinician", "오늘 표정이 좀 어두워 보이시네요"),
    ("patient", "에스시탈로프람 10mg 먹고 있어요"),
    ("patient", "약 먹으면 속이 울렁거려요"),
    ("patient", "회사에서 집중이 안 돼요"),
    ("clinician", "혹시 죽고 싶다는 생각이 드세요?"),
    ("patient", "그런 생각까지는 없어요"),
    ("patient", "일주일에 두 번 소주 한 병 마셔요"),
    ("clinician", "목소리에 힘이 없어 보입니다"),
    ("clinician", "에스시탈로프람을 15mg으로 올려보겠습니다"),
    ("clinician", "수면일지를 써 오세요"),
    ("clinician", "2주 뒤에 뵙겠습니다"),
)


def test_helpers_build_valid_models():
    assert [s.seq for s in CONSULTATION] == list(range(1, len(CONSULTATION) + 1))
    st = statement("S", "입맛이 없다고 함", (4, "입맛이 없어요"))
    assert draft(st).statements[0].kind == "reported"
    assert context(CONSULTATION).max_per_section == 12
