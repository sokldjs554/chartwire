"""Blind index for exact-match lookups on encrypted columns (§3.1, §8.2, Q6).

``blind_index = HMAC-SHA256(HKDF(master, info=f"bidx:{tenant_id}"), normalize(value))``.
The per-tenant HKDF key means identical names in two tenants produce unrelated
digests, so the index leaks nothing across tenants even without RLS.
"""

from __future__ import annotations

import hashlib
import hmac
import unicodedata
from uuid import UUID

from chartwire.crypto.kek import hkdf_sha256


def normalize(value: str) -> str:
    """NFKC → strip → lower, so full-width/compatibility forms and case do not split the index."""
    return unicodedata.normalize("NFKC", value).strip().lower()


def blind_index_key(master: bytes, tenant_id: UUID | str) -> bytes:
    return hkdf_sha256(master, f"bidx:{tenant_id}")


def blind_index(master: bytes, tenant_id: UUID | str, value: str) -> bytes:
    return hmac.new(
        blind_index_key(master, tenant_id), normalize(value).encode("utf-8"), hashlib.sha256
    ).digest()
