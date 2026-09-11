"""RBAC matrix shape (§6.9 coverage), the pure ``check`` decision and the ``require`` dependency."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from chartwire.auth import rbac
from chartwire.auth.deps import current_principal
from chartwire.auth.jwt import Principal

# Every row of spec §6.9, transcribed independently of rbac.MATRIX so a typo in either side shows up.
SPEC_ROUTES: dict[str, set[str]] = {
    "POST /v1/auth/token": set(),
    "POST /v1/consultations": set(),
    "GET /v1/me": {"clinician", "staff", "admin", "auditor", "recorder"},
    "POST /v1/users": {"admin"},
    "GET /v1/users": {"admin"},
    "POST /v1/patients": {"clinician", "staff"},
    "GET /v1/patients": {"clinician", "staff"},
    "GET /v1/patients/{id}": {"clinician", "staff"},
    "POST /v1/patients/{id}/consents": {"clinician", "staff"},
    "POST /v1/consents/{id}/revoke": {"clinician", "staff", "admin"},
    "GET /v1/patients/{id}/consents": {"clinician", "staff"},
    "POST /v1/sessions": {"clinician", "staff"},
    "GET /v1/sessions": {"clinician", "staff", "admin"},
    "GET /v1/sessions/{id}": {"clinician", "staff"},
    "POST /v1/sessions/{id}/ws-ticket": {"clinician", "staff", "recorder"},
    "POST /v1/sessions/{id}/end": {"clinician", "staff"},
    "GET /v1/sessions/{id}/segments": {"clinician", "staff"},
    "GET /v1/patients/{id}/timeline": {"clinician", "staff"},
    "GET /v1/search": {"clinician", "staff"},
    "GET /v1/alerts": {"clinician", "staff"},
    "POST /v1/alerts/{id}/ack": {"clinician", "staff"},
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
    "GET /healthz": set(),
    "GET /readyz": set(),
    "GET /metrics": set(),
}


def _principal(role: str) -> Principal:
    return Principal(sub=str(uuid4()), tenant_id=uuid4(), role=role, jti="j", exp=datetime.now(tz=UTC))


def test_matrix_matches_spec_route_list_exactly() -> None:
    assert rbac.MATRIX == SPEC_ROUTES


def test_matrix_keys_are_well_formed_and_roles_known() -> None:
    key_re = re.compile(r"^(GET|POST|PUT|DELETE|PATCH) /[a-z0-9/{}\-]*$")
    for key, roles in rbac.MATRIX.items():
        assert key_re.match(key), key
        assert roles <= rbac.USER_ROLES, key
    assert "service" not in {r for roles in rbac.MATRIX.values() for r in roles}


@pytest.mark.parametrize("key", sorted(rbac.MATRIX))
@pytest.mark.parametrize("role", sorted(rbac.USER_ROLES))
def test_check_agrees_with_matrix_for_every_route_and_role(key: str, role: str) -> None:
    method, path = key.split(" ", 1)
    expected = not rbac.MATRIX[key] or role in rbac.MATRIX[key]
    assert rbac.check(_principal(role), method, path) is expected


def test_check_anonymous_only_on_public_routes() -> None:
    assert rbac.check(None, "GET", "/healthz") is True
    assert rbac.check(None, "get", "/v1/me") is False
    assert rbac.is_public("POST", "/v1/auth/token") is True
    assert rbac.is_public("GET", "/v1/me") is False


def test_check_unknown_route_fails_closed_loudly() -> None:
    with pytest.raises(KeyError):
        rbac.check(_principal("admin"), "GET", "/v1/not-a-route")


def test_require_factory_validates_roles() -> None:
    with pytest.raises(ValueError, match="at least one"):
        rbac.require()
    with pytest.raises(ValueError, match="unknown roles"):
        rbac.require("clinician", "root")


def test_require_dependency_allows_and_forbids() -> None:
    app = FastAPI()

    @app.get("/only-admin")
    def only_admin(p: Principal = Depends(rbac.require("admin"))) -> dict[str, str]:
        return {"role": p.role}

    current: dict[str, Principal] = {"p": _principal("admin")}
    app.dependency_overrides[current_principal] = lambda: current["p"]
    client = TestClient(app)

    assert client.get("/only-admin").json() == {"role": "admin"}
    current["p"] = _principal("clinician")
    resp = client.get("/only-admin")
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "CW-4030"
