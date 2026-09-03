"""The single source of Redis key names (spec §5). No other module writes a key literal.

TTLs are seconds; ``None`` = no expiry. Stream field names and the consumer-group name are
part of the contract shared by the api (producer) and the stt-worker (consumer).
"""

from __future__ import annotations

from typing import Final
from uuid import UUID

TTL_SESSION_AFTER_END: Final = 24 * 3600
TTL_VIEWERS: Final = 24 * 3600
TTL_STT_OWNER_MS: Final = 30_000
TTL_TICKET: Final = 30
TTL_RATELIMIT: Final = 60
TTL_IDEMPOTENCY: Final = 24 * 3600
TTL_NODE: Final = 30

STT_CONSUMER_GROUP: Final = "stt"
CHUNK_FIELDS: Final = ("seq", "key", "len", "off", "fl", "ep", "ts")
END_MARKER: Final = {"end": "1"}

STT_ACTIVE: Final = "stt:active"
STT_LAG: Final = "stt:lag"
ALERTS_SLA: Final = "alerts:sla"
KEYS_INVALIDATE: Final = "keys:invalidate"
OUTBOX_WAKE: Final = "outbox:wake"


def sess(sid: UUID | str) -> str:
    """HASH {epoch, state, ledger_seq, ack_seq, credit, node, conn, started_at, stt_lag, updated_at}."""
    return f"sess:{sid}"


def sess_chunks(sid: UUID | str) -> str:
    """STREAM api → stt-worker (metadata only, no audio bytes)."""
    return f"sess:{sid}:chunks"


def sess_events(sid: UUID | str) -> str:
    """PUB/SUB fan-out to viewers."""
    return f"sess:{sid}:events"


def sess_viewers(sid: UUID | str) -> str:
    """SET of viewer conn_ids (presence)."""
    return f"sess:{sid}:viewers"


def sess_pattern(sid: UUID | str) -> str:
    """SCAN/DEL pattern covering every per-session key (purge, verify)."""
    return f"sess:{sid}*"


def ctl(sid: UUID | str) -> str:
    """PUB/SUB control channel: superseded / consent_revoked / purge."""
    return f"ctl:{sid}"


def stt_owner(sid: UUID | str) -> str:
    """STRING worker_id lease (SET NX PX 30000)."""
    return f"stt:owner:{sid}"


def tenant_alerts(tenant_id: UUID | str) -> str:
    """PUB/SUB tenant-wide alert escalations (dashboard)."""
    return f"tenant:{tenant_id}:alerts"


def alerts_sla_member(tenant_id: UUID | str, risk_event_id: int) -> str:
    return f"{tenant_id}:{risk_event_id}"


def ticket(token: str) -> str:
    """STRING json ticket payload, consumed with GETDEL."""
    return f"ticket:{token}"


def ratelimit(tenant_id: UUID | str, principal: UUID | str, bucket: str, minute: int) -> str:
    return f"rl:{tenant_id}:{principal}:{bucket}:{minute}"


def idempotency(tenant_id: UUID | str, key: str) -> str:
    return f"idem:{tenant_id}:{key}"


def node(node_id: str) -> str:
    """HASH {conns, started_at} heartbeat."""
    return f"node:{node_id}"
