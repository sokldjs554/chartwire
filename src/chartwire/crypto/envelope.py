"""AES-256-GCM envelope primitives (§3.1, §8.2).

Blob layout: ``nonce(12) || ciphertext || tag(16)``. The AAD string produced by
:func:`aad` binds every ciphertext to its tenant, scope, row and column, so a
blob copied into another row (or another column of the same row) fails to
authenticate — the "row swap" mitigation of the threat model.
"""

from __future__ import annotations

import hashlib
import os
from typing import Final
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from chartwire.crypto.errors import DecryptError

DEK_LEN: Final = 32
NONCE_LEN: Final = 12
TAG_LEN: Final = 16
MIN_BLOB_LEN: Final = NONCE_LEN + TAG_LEN


def aad(tenant_id: UUID | str, scope: str, scope_id: UUID | str, field: str) -> str:
    """Associated data for one encrypted column of one row: ``tenant:scope:scope_id:field``."""
    return f"{tenant_id}:{scope}:{scope_id}:{field}"


def dek_fingerprint(wrapped: bytes) -> bytes:
    """SHA-256 of the *wrapped* DEK; stored next to it and quoted in the purge receipt (§8.4)."""
    return hashlib.sha256(wrapped).digest()


def _check_dek(dek: bytes) -> None:
    if len(dek) != DEK_LEN:
        raise ValueError(f"dek must be {DEK_LEN} bytes")


class Envelope:
    """Stateless AES-256-GCM helpers. All methods are static so callers never hold key state here."""

    @staticmethod
    def new_dek() -> bytes:
        return os.urandom(DEK_LEN)

    @staticmethod
    def encrypt(dek: bytes, plaintext: bytes, aad: str) -> bytes:
        _check_dek(dek)
        nonce = os.urandom(NONCE_LEN)
        return nonce + AESGCM(dek).encrypt(nonce, plaintext, aad.encode("utf-8"))

    @staticmethod
    def decrypt(dek: bytes, blob: bytes, aad: str) -> bytes:
        _check_dek(dek)
        if len(blob) < MIN_BLOB_LEN:
            raise DecryptError("malformed_blob")
        nonce, body = blob[:NONCE_LEN], blob[NONCE_LEN:]
        try:
            return AESGCM(dek).decrypt(nonce, body, aad.encode("utf-8"))
        except InvalidTag as exc:
            raise DecryptError("invalid_tag") from exc
