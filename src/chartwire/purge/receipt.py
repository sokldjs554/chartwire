"""파기 영수증 (purge receipt) — the JSON view of a ``purge_jobs`` row and its hash (spec §8.4 step 6).

``receipt_hash = sha256(canonical_json({steps, counts, dek_fingerprints}))``. Canonical means sorted
keys, no whitespace, UTF-8, so anyone holding the receipt can recompute the hash from the visible
fields. The artifact is called a *receipt*: it attests what the pipeline deleted from live tables,
the object store and Redis, and that the DEK was destroyed — not what WAL, base backups or dead
tuples still hold (ADR-0004, ``docs/consent-purge.md``).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any
from uuid import UUID

from chartwire.db.models import PurgeJob

RECEIPT_FIELDS = ("steps", "counts", "dek_fingerprints")


def _default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, bytes):
        return value.hex()
    raise TypeError(f"not JSON serialisable: {type(value).__name__}")


def canonical_json(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=_default).encode(
        "utf-8"
    )


def receipt_hash(steps: list[dict[str, Any]], counts: dict[str, Any], dek_fingerprints: list[str]) -> bytes:
    payload = {"steps": steps, "counts": counts, "dek_fingerprints": dek_fingerprints}
    return hashlib.sha256(canonical_json(payload)).digest()


def build(job: PurgeJob) -> dict[str, Any]:
    """``PurgeReceiptOut`` payload. ``receipt_hash_valid`` recomputes the hash from the stored fields."""
    stored = job.receipt_hash
    valid: bool | None = None
    if stored is not None:
        valid = receipt_hash(list(job.steps), dict(job.counts), list(job.dek_fingerprints)) == bytes(stored)
    return {
        "id": job.id,
        "tenant_id": job.tenant_id,
        "subject_type": job.subject_type,
        "subject_id": job.subject_id,
        "reason": job.reason,
        "state": job.state,
        "requested_by": job.requested_by,
        "requested_at": job.requested_at,
        "completed_at": job.completed_at,
        "verified_at": job.verified_at,
        "steps": list(job.steps),
        "counts": dict(job.counts),
        "dek_fingerprints": list(job.dek_fingerprints),
        "receipt_hash": None if stored is None else bytes(stored).hex(),
        "receipt_hash_valid": valid,
        "verify_result": job.verify_result,
    }
