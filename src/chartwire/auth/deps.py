"""FastAPI dependencies that turn ``Authorization: Bearer <JWT>`` into a :class:`Principal` (§8.1).

The signing secret and the clock are themselves dependencies so tests and the
application factory can override them (``app.dependency_overrides[jwt_secret]``)
without touching process environment.
"""

from __future__ import annotations

import os
from typing import Final

from fastapi import Depends, Header, HTTPException, status

from chartwire.auth.jwt import SYSTEM_CLOCK, Clock, Principal, TokenError, verify

JWT_SECRET_ENV: Final = "CHARTWIRE_JWT_SECRET"
_BEARER: Final = "bearer"
_UNAUTHORIZED_HEADERS: Final = {"WWW-Authenticate": "Bearer"}


def jwt_secret() -> str:
    """Default secret source: the ``CHARTWIRE_JWT_SECRET`` environment variable."""
    secret = os.environ.get(JWT_SECRET_ENV, "")
    if not secret:
        raise RuntimeError(f"{JWT_SECRET_ENV} is not set")
    return secret


def clock() -> Clock:
    return SYSTEM_CLOCK


def unauthorized(reason: str) -> HTTPException:
    """401 with a stable machine-readable reason; never echoes the token."""
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"code": "CW-4010", "detail": "인증 토큰이 없거나 유효하지 않습니다.", "reason": reason},
        headers=_UNAUTHORIZED_HEADERS,
    )


def parse_bearer(authorization: str | None) -> str:
    """Extract the token from an ``Authorization`` header value; raises 401 on any malformation."""
    if not authorization:
        raise unauthorized("missing_token")
    scheme, _, token = authorization.strip().partition(" ")
    token = token.strip()
    if scheme.lower() != _BEARER or not token or " " in token:
        raise unauthorized("bad_scheme")
    return token


def current_principal(
    authorization: str | None = Header(default=None),
    secret: str = Depends(jwt_secret),
    clk: Clock = Depends(clock),
) -> Principal:
    token = parse_bearer(authorization)
    try:
        return verify(token, secret=secret, clock=clk)
    except TokenError as exc:
        raise unauthorized(exc.reason) from exc
