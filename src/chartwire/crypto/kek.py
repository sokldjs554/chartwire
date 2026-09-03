"""Key-encryption-key providers (§3.1, §8.2).

``LocalKek`` derives one KEK per ``kek_ref`` from the process master secret with
HKDF-SHA256 and wraps DEKs with AES-256-GCM (``kek_ref`` doubles as AAD, so a
wrapped DEK cannot be replayed under a different tenant's reference).
``AwsKmsKek`` maps the same interface onto KMS ``Encrypt``/``Decrypt`` with the
``kek_ref`` as encryption context; boto3 is imported lazily and the class is not
exercised offline (§8.2: KMS testing and key rotation are out of scope).
"""

from __future__ import annotations

import base64
import binascii
import os
from typing import Any, Final, Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from chartwire.crypto.envelope import DEK_LEN, MIN_BLOB_LEN, NONCE_LEN
from chartwire.crypto.errors import DecryptError, KekUnavailableError

MASTER_LEN: Final = 32
MASTER_ENV: Final = "CHARTWIRE_KEK_MASTER"


class KekProvider(Protocol):
    def wrap(self, dek: bytes, kek_ref: str) -> bytes: ...

    def unwrap(self, wrapped: bytes, kek_ref: str) -> bytes: ...


def hkdf_sha256(master: bytes, info: str, length: int = DEK_LEN) -> bytes:
    """HKDF-SHA256 with an empty salt; ``info`` separates every derived key by purpose."""
    if len(master) != MASTER_LEN:
        raise ValueError(f"master must be {MASTER_LEN} bytes")
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=None, info=info.encode("utf-8")).derive(master)


def master_from_base64(encoded: str) -> bytes:
    try:
        master = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise KekUnavailableError("kek master is not valid base64") from exc
    if len(master) != MASTER_LEN:
        raise KekUnavailableError(f"kek master must decode to {MASTER_LEN} bytes")
    return master


class LocalKek:
    """KEK = HKDF-SHA256(master, info=kek_ref); wrap = AES-256-GCM(kek, dek, aad=kek_ref)."""

    def __init__(self, master: bytes) -> None:
        if len(master) != MASTER_LEN:
            raise ValueError(f"master must be {MASTER_LEN} bytes")
        self._master = master

    @classmethod
    def from_base64(cls, encoded: str) -> LocalKek:
        return cls(master_from_base64(encoded))

    @classmethod
    def from_env(cls, name: str = MASTER_ENV) -> LocalKek:
        value = os.environ.get(name)
        if not value:
            raise KekUnavailableError(f"{name} is not set")
        return cls.from_base64(value)

    def derive(self, kek_ref: str) -> bytes:
        if not kek_ref:
            raise ValueError("kek_ref must be non-empty")
        return hkdf_sha256(self._master, kek_ref)

    def wrap(self, dek: bytes, kek_ref: str) -> bytes:
        if len(dek) != DEK_LEN:
            raise ValueError(f"dek must be {DEK_LEN} bytes")
        nonce = os.urandom(NONCE_LEN)
        return nonce + AESGCM(self.derive(kek_ref)).encrypt(nonce, dek, kek_ref.encode("utf-8"))

    def unwrap(self, wrapped: bytes, kek_ref: str) -> bytes:
        if len(wrapped) < MIN_BLOB_LEN:
            raise DecryptError("malformed_blob")
        try:
            return AESGCM(self.derive(kek_ref)).decrypt(
                wrapped[:NONCE_LEN], wrapped[NONCE_LEN:], kek_ref.encode("utf-8")
            )
        except InvalidTag as exc:
            raise DecryptError("invalid_tag") from exc


class _KmsClient(Protocol):
    def encrypt(self, **kwargs: Any) -> dict[str, Any]: ...

    def decrypt(self, **kwargs: Any) -> dict[str, Any]: ...


class AwsKmsKek:
    """KMS-backed provider. ``kek_ref`` is the KMS key id/alias and is also bound as encryption context."""

    def __init__(self, client: _KmsClient | None = None, *, region: str | None = None) -> None:
        if client is None:
            try:
                import boto3
            except ImportError as exc:  # pragma: no cover - depends on the optional extra
                raise KekUnavailableError(
                    "boto3 is required for AwsKmsKek (pip install 'chartwire[aws]')"
                ) from exc
            client = boto3.client("kms", region_name=region)
        self._client = client

    def wrap(self, dek: bytes, kek_ref: str) -> bytes:
        if len(dek) != DEK_LEN:
            raise ValueError(f"dek must be {DEK_LEN} bytes")
        resp = self._client.encrypt(KeyId=kek_ref, Plaintext=dek, EncryptionContext={"kek_ref": kek_ref})
        return bytes(resp["CiphertextBlob"])

    def unwrap(self, wrapped: bytes, kek_ref: str) -> bytes:
        try:
            resp = self._client.decrypt(
                CiphertextBlob=wrapped, KeyId=kek_ref, EncryptionContext={"kek_ref": kek_ref}
            )
        except Exception as exc:  # botocore raises ClientError subclasses built at runtime
            raise DecryptError("kms_decrypt_failed") from exc
        return bytes(resp["Plaintext"])
