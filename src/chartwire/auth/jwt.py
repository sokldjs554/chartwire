"""HS256 JSON Web Tokens without a JWT library (§8.1).

Only the subset chartwire needs is implemented: compact serialization, ``HS256``
signatures with :mod:`hmac`, and the fixed claim set ``{sub, tid, role, jti, exp, iat}``.
The header is pinned to ``{"alg": "HS256", "typ": "JWT"}``: a token whose header
names any other algorithm (``none``, ``RS256`` …) is rejected before the
signature is even looked at, which closes the classic "algorithm confusion"
attack. Signature comparison is constant-time (:func:`hmac.compare_digest`).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Protocol
from uuid import UUID

ALG: Final = "HS256"
HEADER: Final = {"alg": ALG, "typ": "JWT"}
DEFAULT_TTL: Final = timedelta(minutes=15)
ROLES: Final = frozenset({"clinician", "staff", "admin", "auditor", "recorder", "service"})
_REQUIRED_CLAIMS: Final = ("sub", "tid", "role", "jti", "exp")


class Clock(Protocol):
    """Structural twin of ``chartwire.core.clock.Clock`` (only ``now`` is needed here)."""

    def now(self) -> datetime: ...


class _SystemClock:
    def now(self) -> datetime:
        return datetime.now(tz=UTC)


SYSTEM_CLOCK: Final = _SystemClock()


class TokenError(Exception):
    """A token could not be verified. ``reason`` is a stable, PHI-free identifier for logs/metrics."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class Principal:
    """The verified identity behind a request or WebSocket connection."""

    sub: str
    tenant_id: UUID
    role: str
    jti: str
    exp: datetime

    @property
    def user_id(self) -> UUID | None:
        """``sub`` as a UUID when it is one (dev tokens without ``--user`` carry ``dev:<role>``)."""
        try:
            return UUID(self.sub)
        except ValueError:
            return None


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(segment: str) -> bytes:
    padding = "=" * (-len(segment) % 4)
    try:
        return base64.urlsafe_b64decode(segment + padding)
    except (binascii.Error, ValueError) as exc:
        raise TokenError("malformed") from exc


def _json_segment(obj: Mapping[str, Any]) -> str:
    return _b64url_encode(json.dumps(obj, separators=(",", ":"), sort_keys=True).encode("utf-8"))


def _sign(signing_input: bytes, secret: str) -> bytes:
    return hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()


def issue(
    claims: Mapping[str, Any],
    ttl: timedelta = DEFAULT_TTL,
    *,
    secret: str,
    clock: Clock = SYSTEM_CLOCK,
) -> str:
    """Sign ``claims`` (``sub``, ``tid``, ``role`` required; ``jti`` generated when absent) valid for ``ttl``."""
    if not secret:
        raise ValueError("jwt secret must be non-empty")
    if ttl <= timedelta(0):
        raise ValueError("ttl must be positive")
    role = str(claims["role"])
    if role not in ROLES:
        raise ValueError(f"unknown role: {role}")
    now = clock.now()
    payload = {
        "sub": str(claims["sub"]),
        "tid": str(UUID(str(claims["tid"]))),
        "role": role,
        "jti": str(claims.get("jti") or secrets.token_urlsafe(16)),
        "iat": int(now.timestamp()),
        "exp": int((now + ttl).timestamp()),
    }
    signing_input = f"{_json_segment(HEADER)}.{_json_segment(payload)}".encode("ascii")
    return f"{signing_input.decode('ascii')}.{_b64url_encode(_sign(signing_input, secret))}"


def verify(token: str, *, secret: str, clock: Clock = SYSTEM_CLOCK) -> Principal:
    """Validate header, signature, expiry and claim shape; return the :class:`Principal`."""
    parts = token.split(".")
    if len(parts) != 3 or not all(parts):
        raise TokenError("malformed")
    header_b64, payload_b64, signature_b64 = parts
    header = _decode_object(header_b64)
    if header.get("alg") != ALG or header.get("typ") != "JWT":
        raise TokenError("alg_mismatch")
    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    if not hmac.compare_digest(_sign(signing_input, secret), _b64url_decode(signature_b64)):
        raise TokenError("bad_signature")
    payload = _decode_object(payload_b64)
    if any(claim not in payload for claim in _REQUIRED_CLAIMS):
        raise TokenError("missing_claim")
    exp = payload["exp"]
    if not isinstance(exp, int) or isinstance(exp, bool):
        raise TokenError("malformed")
    expires_at = datetime.fromtimestamp(exp, tz=UTC)
    if clock.now() >= expires_at:
        raise TokenError("expired")
    role = payload["role"]
    if role not in ROLES:
        raise TokenError("unknown_role")
    try:
        tenant_id = UUID(str(payload["tid"]))
    except ValueError as exc:
        raise TokenError("malformed") from exc
    return Principal(
        sub=str(payload["sub"]), tenant_id=tenant_id, role=role, jti=str(payload["jti"]), exp=expires_at
    )


def _decode_object(segment: str) -> dict[str, Any]:
    try:
        obj = json.loads(_b64url_decode(segment))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TokenError("malformed") from exc
    if not isinstance(obj, dict):
        raise TokenError("malformed")
    return obj
