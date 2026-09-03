"""Exception hierarchy of the envelope-encryption layer.

Every error deliberately carries no key material and no plaintext in its message
(§0.9: ciphertext keys are never logged).
"""


class CryptoError(Exception):
    """Base class for all chartwire.crypto errors."""


class DecryptError(CryptoError):
    """Authenticated decryption failed (wrong key, wrong AAD, tampered blob, malformed blob)."""

    def __init__(self, reason: str = "invalid_tag") -> None:
        super().__init__(reason)
        self.reason = reason


class DekDestroyedError(CryptoError):
    """The DEK for this scope was crypto-shredded (`dek_wrapped IS NULL`); nothing can be unwrapped."""

    def __init__(self, scope_id: str) -> None:
        super().__init__(f"dek destroyed for scope {scope_id}")
        self.scope_id = scope_id


class KekUnavailableError(CryptoError):
    """A KEK provider cannot be constructed (missing master, missing optional dependency, ...)."""
