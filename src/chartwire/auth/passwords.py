"""Password hashing with :func:`hashlib.scrypt` (§8.1) — no extra dependency.

Stored format: ``scrypt$<n>$<r>$<p>$<salt_b64>$<hash_b64>``. The parameters travel
with the hash so they can be raised later without invalidating existing rows.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from typing import Final

SCHEME: Final = "scrypt"
N: Final = 2**14
R: Final = 8
P: Final = 1
SALT_LEN: Final = 16
KEY_LEN: Final = 32
_MAXMEM: Final = 64 * 1024 * 1024
_FIELDS: Final = 6


def _derive(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, maxmem=_MAXMEM, dklen=KEY_LEN)


def hash(password: str) -> str:  # the spec names the function ``passwords.hash``
    if not password:
        raise ValueError("password must be non-empty")
    salt = secrets.token_bytes(SALT_LEN)
    digest = _derive(password, salt, N, R, P)
    return "$".join(
        (
            SCHEME,
            str(N),
            str(R),
            str(P),
            base64.b64encode(salt).decode("ascii"),
            base64.b64encode(digest).decode("ascii"),
        )
    )


def verify(password: str, stored: str) -> bool:
    """Constant-time comparison; any malformed ``stored`` value verifies as False (never raises)."""
    parts = stored.split("$")
    if len(parts) != _FIELDS or parts[0] != SCHEME:
        return False
    try:
        n, r, p = (int(v) for v in parts[1:4])
        salt = base64.b64decode(parts[4], validate=True)
        expected = base64.b64decode(parts[5], validate=True)
        actual = _derive(password, salt, n, r, p)
    except ValueError:
        return False
    return hmac.compare_digest(actual, expected)
