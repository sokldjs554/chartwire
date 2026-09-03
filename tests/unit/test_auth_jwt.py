"""HS256 JWT: round trip, expiry with an injected clock, signature/alg/claim rejection."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from chartwire.auth.jwt import ROLES, Principal, TokenError, issue, verify

SECRET = "unit-test-secret"
TENANT = UUID("11111111-1111-4111-8111-111111111111")


class FrozenClock:
    def __init__(self, at: datetime) -> None:
        self.at = at

    def now(self) -> datetime:
        return self.at


T0 = datetime(2026, 9, 2, 9, 0, tzinfo=UTC)


def _claims(**over: object) -> dict[str, object]:
    base: dict[str, object] = {"sub": str(uuid4()), "tid": TENANT, "role": "clinician"}
    base.update(over)
    return base


def _b64url(obj: object) -> str:
    raw = json.dumps(obj, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def test_round_trip_yields_principal() -> None:
    clock = FrozenClock(T0)
    claims = _claims(jti="jti-1")
    token = issue(claims, timedelta(minutes=15), secret=SECRET, clock=clock)
    principal = verify(token, secret=SECRET, clock=clock)
    assert principal == Principal(
        sub=str(claims["sub"]),
        tenant_id=TENANT,
        role="clinician",
        jti="jti-1",
        exp=T0 + timedelta(minutes=15),
    )
    assert principal.user_id == UUID(str(claims["sub"]))


def test_token_is_compact_hs256_with_pinned_header() -> None:
    token = issue(_claims(), secret=SECRET, clock=FrozenClock(T0))
    header_b64, payload_b64, sig_b64 = token.split(".")
    header = json.loads(base64.urlsafe_b64decode(header_b64 + "=="))
    payload = json.loads(base64.urlsafe_b64decode(payload_b64 + "=="))
    assert header == {"alg": "HS256", "typ": "JWT"}
    assert set(payload) == {"sub", "tid", "role", "jti", "iat", "exp"}
    assert payload["exp"] - payload["iat"] == 15 * 60
    assert "=" not in token and len(base64.urlsafe_b64decode(sig_b64 + "==")) == 32


def test_dev_sub_without_uuid_has_no_user_id() -> None:
    token = issue(_claims(sub="dev:clinician"), secret=SECRET, clock=FrozenClock(T0))
    assert verify(token, secret=SECRET, clock=FrozenClock(T0)).user_id is None


def test_expired_token_rejected_by_injected_clock() -> None:
    token = issue(_claims(), timedelta(minutes=15), secret=SECRET, clock=FrozenClock(T0))
    assert verify(token, secret=SECRET, clock=FrozenClock(T0 + timedelta(minutes=14, seconds=59)))
    with pytest.raises(TokenError, match="expired"):
        verify(token, secret=SECRET, clock=FrozenClock(T0 + timedelta(minutes=15)))


def test_wrong_secret_rejected() -> None:
    token = issue(_claims(), secret=SECRET, clock=FrozenClock(T0))
    with pytest.raises(TokenError, match="bad_signature"):
        verify(token, secret="other-secret", clock=FrozenClock(T0))


def test_payload_tamper_rejected() -> None:
    token = issue(_claims(role="staff"), secret=SECRET, clock=FrozenClock(T0))
    header, payload, sig = token.split(".")
    forged = json.loads(base64.urlsafe_b64decode(payload + "=="))
    forged["role"] = "admin"
    with pytest.raises(TokenError, match="bad_signature"):
        verify(f"{header}.{_b64url(forged)}.{sig}", secret=SECRET, clock=FrozenClock(T0))


@pytest.mark.parametrize("alg", ["none", "HS512", "RS256", "hs256"])
def test_algorithm_confusion_rejected_before_signature_check(alg: str) -> None:
    token = issue(_claims(), secret=SECRET, clock=FrozenClock(T0))
    _, payload, sig = token.split(".")
    with pytest.raises(TokenError, match="alg_mismatch"):
        verify(f"{_b64url({'alg': alg, 'typ': 'JWT'})}.{payload}.{sig}", secret=SECRET)
    with pytest.raises(TokenError, match=r"alg_mismatch|malformed"):
        verify(f"{_b64url({'alg': alg, 'typ': 'JWT'})}.{payload}.", secret=SECRET)


@pytest.mark.parametrize("bad", ["", "abc", "a.b", "a.b.c.d", "..", "!!.!!.!!"])
def test_malformed_tokens_rejected(bad: str) -> None:
    with pytest.raises(TokenError, match=r"malformed|alg_mismatch"):
        verify(bad, secret=SECRET)


def test_missing_claim_rejected_even_with_valid_signature() -> None:
    import hashlib
    import hmac

    header = _b64url({"alg": "HS256", "typ": "JWT"})
    payload = _b64url({"sub": "x", "tid": str(TENANT), "role": "clinician", "exp": 4_000_000_000})
    sig = hmac.new(SECRET.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest()
    token = f"{header}.{payload}.{base64.urlsafe_b64encode(sig).rstrip(b'=').decode()}"
    with pytest.raises(TokenError, match="missing_claim"):
        verify(token, secret=SECRET, clock=FrozenClock(T0))


def test_issue_validates_inputs() -> None:
    with pytest.raises(ValueError, match="unknown role"):
        issue(_claims(role="superuser"), secret=SECRET)
    with pytest.raises(ValueError, match=r"tid|hexadecimal|badly formed"):
        issue(_claims(tid="not-a-uuid"), secret=SECRET)
    with pytest.raises(ValueError, match="secret"):
        issue(_claims(), secret="")
    with pytest.raises(ValueError, match="ttl"):
        issue(_claims(), timedelta(0), secret=SECRET)


def test_roles_cover_every_db_role_plus_service() -> None:
    assert {"clinician", "staff", "admin", "auditor", "recorder", "service"} == ROLES
