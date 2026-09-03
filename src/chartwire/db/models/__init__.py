"""SQLAlchemy 2.0 models mirroring the DDL executed by the migrations (spec §4.2).

Importing this package registers every table on ``Base.metadata``; migrations are
hand-written (autogenerate cannot model partitions, RLS or triggers), so the models are the
typed view used by the repositories, not the source of the DDL.
"""

from chartwire.db.base import Base
from chartwire.db.models.notes import Note, NoteAssessment, NoteStatement
from chartwire.db.models.ops import AuditEvent, DeadLetter, OutboxEvent, ProcessedEvent, PurgeJob
from chartwire.db.models.patients import Consent, Patient
from chartwire.db.models.risk import RiskEvent
from chartwire.db.models.segments import SegmentSearch, TranscriptSegment
from chartwire.db.models.sessions import AudioChunk, Session, SttOffset
from chartwire.db.models.tenancy import Tenant, User

TENANT_TABLES: tuple[str, ...] = (
    "users",
    "patients",
    "consents",
    "sessions",
    "audio_chunks",
    "stt_offsets",
    "transcript_segments",
    "segment_search",
    "risk_events",
    "notes",
    "note_statements",
    "note_assessments",
    "outbox_events",
    "processed_events",
    "dead_letters",
    "audit_events",
    "purge_jobs",
)
"""Every table carrying ``tenant_id`` (all RLS-protected); ``tenants`` itself is not listed."""

__all__ = [
    "TENANT_TABLES",
    "AudioChunk",
    "AuditEvent",
    "Base",
    "Consent",
    "DeadLetter",
    "Note",
    "NoteAssessment",
    "NoteStatement",
    "OutboxEvent",
    "Patient",
    "ProcessedEvent",
    "PurgeJob",
    "RiskEvent",
    "SegmentSearch",
    "Session",
    "SttOffset",
    "Tenant",
    "TranscriptSegment",
    "User",
]
