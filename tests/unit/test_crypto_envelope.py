"""Envelope: property-tested round trip, AAD (row-swap) rejection, bit-flip tampering, layout."""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from chartwire.crypto.envelope import (
    DEK_LEN,
    MIN_BLOB_LEN,
    NONCE_LEN,
    TAG_LEN,
    Envelope,
    aad,
    dek_fingerprint,
)
from chartwire.crypto.errors import DecryptError

TENANT = UUID("22222222-2222-4222-8222-222222222222")
_field = st.sampled_from(["text_enc", "name_enc", "phone_enc", "raw_draft_enc"])
_scope = st.sampled_from(["session", "patient", "tenant"])


def _aad_of(tenant: UUID, scope: str, scope_id: UUID, field: str) -> str:
    return aad(tenant, scope, scope_id, field)


@settings(max_examples=300, deadline=None)
@given(plaintext=st.binary(max_size=4096), scope=_scope, field=_field, scope_id=st.uuids())
def test_round_trip(plaintext: bytes, scope: str, field: str, scope_id: UUID) -> None:
    dek = Envelope.new_dek()
    a = _aad_of(TENANT, scope, scope_id, field)
    blob = Envelope.encrypt(dek, plaintext, a)
    assert len(blob) == NONCE_LEN + len(plaintext) + TAG_LEN
    assert Envelope.decrypt(dek, blob, a) == plaintext


@settings(max_examples=200, deadline=None)
@given(
    plaintext=st.binary(min_size=1, max_size=512),
    scope=_scope,
    field=_field,
    row_a=st.uuids(),
    row_b=st.uuids(),
    other_field=_field,
    other_tenant=st.uuids(),
)
def test_aad_mismatch_rejected(
    plaintext: bytes,
    scope: str,
    field: str,
    row_a: UUID,
    row_b: UUID,
    other_field: str,
    other_tenant: UUID,
) -> None:
    """A blob copied to another row / column / tenant (same DEK) must not decrypt."""
    dek = Envelope.new_dek()
    blob = Envelope.encrypt(dek, plaintext, _aad_of(TENANT, scope, row_a, field))
    for wrong in (
        _aad_of(TENANT, scope, row_b, field),
        _aad_of(TENANT, scope, row_a, other_field),
        _aad_of(other_tenant, scope, row_a, field),
    ):
        if wrong == _aad_of(TENANT, scope, row_a, field):
            continue
        with pytest.raises(DecryptError, match="invalid_tag"):
            Envelope.decrypt(dek, blob, wrong)


@settings(max_examples=200, deadline=None)
@given(plaintext=st.binary(min_size=1, max_size=256), data=st.data())
def test_single_bit_flip_anywhere_rejected(plaintext: bytes, data: st.DataObject) -> None:
    dek = Envelope.new_dek()
    a = _aad_of(TENANT, "session", uuid4(), "text_enc")
    blob = bytearray(Envelope.encrypt(dek, plaintext, a))
    pos = data.draw(st.integers(0, len(blob) - 1))
    bit = data.draw(st.integers(0, 7))
    blob[pos] ^= 1 << bit
    with pytest.raises(DecryptError, match="invalid_tag"):
        Envelope.decrypt(dek, bytes(blob), a)


def test_random_nonce_makes_ciphertexts_differ() -> None:
    dek = Envelope.new_dek()
    a = _aad_of(TENANT, "session", uuid4(), "text_enc")
    b1, b2 = Envelope.encrypt(dek, b"same", a), Envelope.encrypt(dek, b"same", a)
    assert b1 != b2 and b1[:NONCE_LEN] != b2[:NONCE_LEN]


def test_wrong_dek_rejected() -> None:
    a = _aad_of(TENANT, "session", uuid4(), "text_enc")
    blob = Envelope.encrypt(Envelope.new_dek(), b"x", a)
    with pytest.raises(DecryptError):
        Envelope.decrypt(Envelope.new_dek(), blob, a)


def test_short_blob_and_bad_dek_length() -> None:
    dek = Envelope.new_dek()
    with pytest.raises(DecryptError, match="malformed_blob"):
        Envelope.decrypt(dek, b"\x00" * (MIN_BLOB_LEN - 1), "a")
    with pytest.raises(ValueError, match="dek"):
        Envelope.encrypt(b"short", b"x", "a")
    with pytest.raises(ValueError, match="dek"):
        Envelope.decrypt(b"short", b"\x00" * MIN_BLOB_LEN, "a")


def test_new_dek_is_32_random_bytes() -> None:
    a, b = Envelope.new_dek(), Envelope.new_dek()
    assert len(a) == DEK_LEN == 32 and a != b


def test_aad_format_is_spec_string() -> None:
    sid = uuid4()
    assert aad(TENANT, "session", sid, "text_enc") == f"{TENANT}:session:{sid}:text_enc"


def test_dek_fingerprint_is_sha256_of_wrapped() -> None:
    import hashlib

    assert dek_fingerprint(b"wrapped") == hashlib.sha256(b"wrapped").digest()
