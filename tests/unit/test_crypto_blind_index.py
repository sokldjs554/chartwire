"""Blind index: determinism, NFKC/case/whitespace normalization, per-tenant separation."""

from __future__ import annotations

import hashlib
import hmac
from uuid import UUID, uuid4

from hypothesis import given, settings
from hypothesis import strategies as st

from chartwire.crypto.blind_index import blind_index, blind_index_key, normalize
from chartwire.crypto.kek import hkdf_sha256

MASTER = bytes(range(32))
TENANT = UUID("33333333-3333-4333-8333-333333333333")


def test_matches_spec_formula() -> None:
    key = hkdf_sha256(MASTER, f"bidx:{TENANT}")
    expected = hmac.new(key, "가상환자-0001".encode(), hashlib.sha256).digest()
    assert blind_index(MASTER, TENANT, "가상환자-0001") == expected
    assert blind_index_key(MASTER, TENANT) == key
    assert len(expected) == 32


def test_normalization_collapses_equivalent_spellings() -> None:
    canonical = blind_index(MASTER, TENANT, "가상환자-0001")
    assert blind_index(MASTER, TENANT, "  가상환자-0001 ") == canonical
    assert blind_index(MASTER, TENANT, "ＧＡＳＡＮＧ") == blind_index(
        MASTER, TENANT, "gasang"
    )  # full-width → NFKC
    assert blind_index(MASTER, TENANT, "한") == blind_index(MASTER, TENANT, "한")  # jamo → syllable
    assert normalize("  Ａ ") == "a ".strip() or normalize("Ａ") == "a"


@settings(max_examples=200, deadline=None)
@given(value=st.text(min_size=1, max_size=40))
def test_deterministic_and_normalized(value: str) -> None:
    once, twice = blind_index(MASTER, TENANT, value), blind_index(MASTER, TENANT, value)
    assert once == twice
    assert blind_index(MASTER, TENANT, normalize(value)) == once
    assert blind_index(MASTER, TENANT, f"  {value}\u3000") == once  # padding incl. ideographic space


def test_tenants_and_masters_do_not_collide() -> None:
    a = blind_index(MASTER, TENANT, "x")
    assert blind_index(MASTER, uuid4(), "x") != a
    assert blind_index(bytes(32), TENANT, "x") != a
    assert blind_index(MASTER, str(TENANT), "x") == a  # UUID or its string form
