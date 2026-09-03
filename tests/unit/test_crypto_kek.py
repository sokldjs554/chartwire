"""KEK providers: LocalKek HKDF derivation/wrapping, master parsing, AwsKmsKek against a fake client."""

from __future__ import annotations

import base64
import os
from typing import Any

import pytest

from chartwire.crypto.envelope import DEK_LEN, Envelope
from chartwire.crypto.errors import DecryptError, KekUnavailableError
from chartwire.crypto.kek import (
    MASTER_ENV,
    MASTER_LEN,
    AwsKmsKek,
    KekProvider,
    LocalKek,
    hkdf_sha256,
    master_from_base64,
)

MASTER = bytes(range(32))
MASTER_B64 = base64.b64encode(MASTER).decode()


def test_local_kek_wrap_unwrap_round_trip() -> None:
    kek = LocalKek(MASTER)
    dek = Envelope.new_dek()
    wrapped = kek.wrap(dek, "tenant-a")
    assert wrapped != dek and len(wrapped) == 12 + DEK_LEN + 16
    assert kek.unwrap(wrapped, "tenant-a") == dek


def test_local_kek_binds_kek_ref() -> None:
    """A wrapped DEK cannot be unwrapped under another tenant's reference (different KEK *and* AAD)."""
    kek = LocalKek(MASTER)
    wrapped = kek.wrap(Envelope.new_dek(), "tenant-a")
    with pytest.raises(DecryptError, match="invalid_tag"):
        kek.unwrap(wrapped, "tenant-b")


def test_local_kek_is_deterministic_per_master_and_ref() -> None:
    assert LocalKek(MASTER).derive("r") == LocalKek(MASTER).derive("r")
    assert LocalKek(MASTER).derive("r") != LocalKek(MASTER).derive("s")
    assert LocalKek(MASTER).derive("r") != LocalKek(bytes(32)).derive("r")
    assert LocalKek(MASTER).derive("r") == hkdf_sha256(MASTER, "r")
    with pytest.raises(ValueError, match="non-empty"):
        LocalKek(MASTER).derive("")


def test_local_kek_rejects_bad_inputs() -> None:
    kek = LocalKek(MASTER)
    with pytest.raises(ValueError, match="master"):
        LocalKek(b"short")
    with pytest.raises(ValueError, match="dek"):
        kek.wrap(b"short", "r")
    with pytest.raises(DecryptError, match="malformed_blob"):
        kek.unwrap(b"\x00" * 10, "r")
    with pytest.raises(ValueError, match="master"):
        hkdf_sha256(b"x", "info")


def test_tampered_wrapped_dek_rejected() -> None:
    kek = LocalKek(MASTER)
    wrapped = bytearray(kek.wrap(Envelope.new_dek(), "r"))
    wrapped[20] ^= 0x01
    with pytest.raises(DecryptError, match="invalid_tag"):
        kek.unwrap(bytes(wrapped), "r")


def test_master_from_base64_and_env(monkeypatch: pytest.MonkeyPatch) -> None:
    assert master_from_base64(MASTER_B64) == MASTER
    assert LocalKek.from_base64(MASTER_B64).derive("r") == LocalKek(MASTER).derive("r")
    with pytest.raises(KekUnavailableError, match="base64"):
        master_from_base64("***")
    with pytest.raises(KekUnavailableError, match=str(MASTER_LEN)):
        master_from_base64(base64.b64encode(b"short").decode())
    monkeypatch.delenv(MASTER_ENV, raising=False)
    with pytest.raises(KekUnavailableError, match=MASTER_ENV):
        LocalKek.from_env()
    monkeypatch.setenv(MASTER_ENV, MASTER_B64)
    assert LocalKek.from_env().derive("r") == LocalKek(MASTER).derive("r")


class FakeKms:
    """Records calls and wraps with a LocalKek so the mapping (KeyId, EncryptionContext) is checked."""

    def __init__(self) -> None:
        self.inner = LocalKek(os.urandom(32))
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def encrypt(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("encrypt", kwargs))
        return {"CiphertextBlob": self.inner.wrap(kwargs["Plaintext"], kwargs["KeyId"])}

    def decrypt(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("decrypt", kwargs))
        return {"Plaintext": self.inner.unwrap(kwargs["CiphertextBlob"], kwargs["KeyId"])}


def test_aws_kms_kek_maps_wrap_unwrap_onto_encrypt_decrypt() -> None:
    fake = FakeKms()
    kek: KekProvider = AwsKmsKek(fake)
    dek = Envelope.new_dek()
    wrapped = kek.wrap(dek, "alias/chartwire-tenant-a")
    assert kek.unwrap(wrapped, "alias/chartwire-tenant-a") == dek
    (op1, enc), (op2, dec) = fake.calls
    assert (op1, op2) == ("encrypt", "decrypt")
    assert enc["KeyId"] == dec["KeyId"] == "alias/chartwire-tenant-a"
    assert enc["EncryptionContext"] == dec["EncryptionContext"] == {"kek_ref": "alias/chartwire-tenant-a"}
    with pytest.raises(ValueError, match="dek"):
        kek.wrap(b"short", "alias/x")


def test_aws_kms_kek_wraps_client_errors_as_decrypt_error() -> None:
    class Failing:
        def encrypt(self, **kwargs: Any) -> dict[str, Any]:
            raise AssertionError("not called")

        def decrypt(self, **kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("AccessDeniedException")

    with pytest.raises(DecryptError, match="kms_decrypt_failed"):
        AwsKmsKek(Failing()).unwrap(b"blob", "alias/x")
