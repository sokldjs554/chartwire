"""PHI envelope encryption: KEK providers, AES-GCM envelope, blind index, DEK cache (§3.1, §8.2)."""

from chartwire.crypto.blind_index import blind_index
from chartwire.crypto.envelope import Envelope, aad, dek_fingerprint
from chartwire.crypto.errors import CryptoError, DecryptError, DekDestroyedError, KekUnavailableError
from chartwire.crypto.kek import AwsKmsKek, KekProvider, LocalKek
from chartwire.crypto.keycache import KeyCache

__all__ = [
    "AwsKmsKek",
    "CryptoError",
    "DecryptError",
    "DekDestroyedError",
    "Envelope",
    "KekProvider",
    "KekUnavailableError",
    "KeyCache",
    "LocalKek",
    "aad",
    "blind_index",
    "dek_fingerprint",
]
