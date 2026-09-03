"""Role-based access control (§8.1, §6.9).

:data:`MATRIX` is the single source of truth for which roles may call which REST
route. Keys are ``"METHOD /v1/path-template"`` exactly as FastAPI registers them
(path parameters as ``{id}``/``{sid}``). ``tests/integration/test_rbac_matrix.py``
iterates every registered route × every role and asserts the HTTP status the
matrix predicts; a route missing from the matrix fails that test, so a new
endpoint cannot silently ship without an access decision.

Ownership rules the spec expresses as "clinician (own)" are *row* constraints
(the clinician may only see their own sessions); they are enforced by the repo
queries and RLS, not by this role gate.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Final

from fastapi import Depends, HTTPException, status

from chartwire.auth.deps import current_principal
from chartwire.auth.jwt import Principal

USER_ROLES: Final = frozenset({"clinician", "staff", "admin", "auditor", "recorder"})
"""Roles a ``users`` row can carry (DB CHECK constraint). ``service`` tokens are worker-only."""

ANY: Final = USER_ROLES
PUBLIC: Final[frozenset[str]] = frozenset()
"""Marker for unauthenticated routes: an empty role set means *no* token is required."""

_CLINICAL: Final = frozenset({"clinician", "staff"})

MATRIX: Final[dict[str, set[str]]] = {
    "POST /v1/auth/token": set(PUBLIC),
    "GET /v1/me": set(ANY),
    "POST /v1/users": {"admin"},
    "GET /v1/users": {"admin"},
    "POST /v1/patients": set(_CLINICAL),
    "GET /v1/patients": set(_CLINICAL),
    "GET /v1/patients/{id}": set(_CLINICAL),
    "POST /v1/patients/{id}/consents": set(_CLINICAL),
    "POST /v1/consents/{id}/revoke": {"clinician", "staff", "admin"},
    "GET /v1/patients/{id}/consents": set(_CLINICAL),
    "POST /v1/sessions": set(_CLINICAL),
    "GET /v1/sessions": {"clinician", "staff", "admin"},
    "GET /v1/sessions/{id}": set(_CLINICAL),
    "POST /v1/sessions/{id}/ws-ticket": {"clinician", "staff", "recorder"},
    "POST /v1/sessions/{id}/end": set(_CLINICAL),
    "GET /v1/sessions/{id}/segments": set(_CLINICAL),
    "GET /v1/patients/{id}/timeline": set(_CLINICAL),
    "GET /v1/search": set(_CLINICAL),
    "GET /v1/alerts": set(_CLINICAL),
    "POST /v1/alerts/{id}/ack": set(_CLINICAL),
    "GET /v1/sessions/{id}/notes/latest": {"clinician", "auditor"},
    "GET /v1/notes/{id}": {"clinician", "auditor"},
    "POST /v1/sessions/{id}/notes/draft": {"clinician"},
    "POST /v1/notes/{id}/statements/{sid}/decision": {"clinician"},
    "PUT /v1/notes/{id}/assessment": {"clinician"},
    "POST /v1/notes/{id}/sign": {"clinician"},
    "POST /v1/purge-jobs": {"admin"},
    "GET /v1/purge-jobs/{id}": {"admin", "auditor"},
    "POST /v1/purge-jobs/{id}/verify-decrypt": {"admin", "auditor"},
    "GET /v1/audit": {"auditor", "admin"},
    "GET /v1/ops/outbox": {"admin"},
    "GET /v1/ops/dead-letters": {"admin"},
    "POST /v1/ops/dead-letters/{id}/replay": {"admin"},
    "GET /v1/ops/partitions": {"admin"},
    "GET /healthz": set(PUBLIC),
    "GET /readyz": set(PUBLIC),
    "GET /metrics": set(PUBLIC),
}


def route_key(method: str, path_template: str) -> str:
    return f"{method.upper()} {path_template}"


def is_public(method: str, path_template: str) -> bool:
    """True when the route needs no token. Raises ``KeyError`` for a route not in the matrix."""
    return not MATRIX[route_key(method, path_template)]


def check(principal: Principal | None, method: str, path_template: str) -> bool:
    """Pure decision: may ``principal`` (None = anonymous) call ``METHOD path_template``?

    Fail-closed on every edge: an unknown route raises ``KeyError`` (so a missing
    matrix row is a loud bug, not a silent allow) and an anonymous caller is only
    admitted to :data:`PUBLIC` routes.
    """
    allowed = MATRIX[route_key(method, path_template)]
    if not allowed:
        return True
    return principal is not None and principal.role in allowed


def forbidden(role: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={"code": "CW-4030", "detail": f"역할 '{role}'에는 이 작업 권한이 없습니다."},
    )


def require(*roles: str) -> Callable[..., Principal]:
    """FastAPI dependency factory: ``Depends(require("clinician", "staff"))``.

    The route's roles are written out at the endpoint *and* in :data:`MATRIX`;
    the RBAC matrix test proves the two agree.
    """
    if not roles:
        raise ValueError("require() needs at least one role")
    unknown = set(roles) - USER_ROLES
    if unknown:
        raise ValueError(f"unknown roles: {sorted(unknown)}")
    allowed = frozenset(roles)

    def dependency(principal: Principal = Depends(current_principal)) -> Principal:
        if principal.role not in allowed:
            raise forbidden(principal.role)
        return principal

    return dependency
