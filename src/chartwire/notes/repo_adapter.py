"""``transcript_segments`` rows → :class:`SegmentView` (spec §9.2, §8.2).

The repository hands back ciphertext (``SegmentRow.text_enc``); the note pipeline works on
plaintext views. This is the one place the two meet: the session DEK comes from the
:class:`KeyCache`, the AAD is the contract shared with the stt-worker (writer) and the viewer
replay (``chartwire.ws.watch.segment_aad``), and a row whose ciphertext does not authenticate
is a hard error — a note must never be drafted over a segment we cannot vouch for.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from chartwire.crypto.envelope import Envelope
from chartwire.db.repo import segments as segments_repo
from chartwire.db.repo.segments import SegmentRow
from chartwire.notes.schema import SegmentView
from chartwire.ws.watch import segment_aad

REPLAY_BATCH = 500
"""Rows per ``segments.replay`` call while loading a whole session (bounded memory, one index scan each)."""


def to_view(row: SegmentRow, *, dek: bytes) -> SegmentView:
    """Decrypt one row. Raises ``DecryptError`` when the blob does not authenticate under this DEK/AAD."""
    plaintext = Envelope.decrypt(dek, row.text_enc, segment_aad(row.tenant_id, row.session_id, row.seq))
    speaker = row.speaker if row.speaker in ("clinician", "patient", "unknown") else "unknown"
    return SegmentView(
        segment_id=row.id,
        seq=row.seq,
        speaker=speaker,  # narrowed above; the DB CHECK guarantees it anyway
        text=plaintext.decode("utf-8"),
        t_start_ms=row.t_start_ms,
        t_end_ms=row.t_end_ms,
        created_at=row.created_at,
        confidence=row.confidence if row.confidence is not None else 0.0,
    )


async def load_segment_views(
    session: AsyncSession,
    *,
    session_id: UUID,
    dek: bytes,
    started_at: datetime | None,
    batch: int = REPLAY_BATCH,
) -> list[SegmentView]:
    """Every final segment of a session in ``seq`` order, decrypted.

    Uses ``repo.segments.replay`` in keyset batches so the planner prunes to the session's
    partition (Q1a) and a long consultation never materialises in one round trip.
    """
    views: list[SegmentView] = []
    after_seq = -1
    while True:
        rows = await segments_repo.replay(session, session_id, after_seq, batch, started_at=started_at)
        views.extend(to_view(row, dek=dek) for row in rows)
        if len(rows) < batch:
            return views
        after_seq = rows[-1].seq


def encrypt_segment_text(*, tenant_id: UUID, session_id: UUID, seq: int, dek: bytes, text: str) -> bytes:
    """Inverse of :func:`to_view` for fixtures and tests (the stt-worker has its own writer path)."""
    return Envelope.encrypt(dek, text.encode("utf-8"), segment_aad(tenant_id, session_id, seq))
