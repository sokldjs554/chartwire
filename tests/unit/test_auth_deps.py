"""``current_principal``: Bearer parsing and 401 mapping, exercised through a real FastAPI app."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from chartwire.auth import deps
from chartwire.auth.jwt import Principal, issue

SECRET = "deps-secret"


class FrozenClock:
    def __init__(self, at: datetime) -> None:
        self.at = at

    def now(self) -> datetime:
        return self.at


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()

    @app.get("/me")
    def me(p: Principal = Depends(deps.current_principal)) -> dict[str, str]:
        return {"sub": p.sub, "role": p.role, "tid": str(p.tenant_id)}

    app.dependency_overrides[deps.jwt_secret] = lambda: SECRET
    return TestClient(app)


def _token(**over: object) -> str:
    claims: dict[str, object] = {"sub": str(uuid4()), "tid": uuid4(), "role": "staff"}
    claims.update(over)
    return issue(claims, secret=SECRET)


def test_valid_bearer_yields_principal(client: TestClient) -> None:
    sub = str(uuid4())
    resp = client.get("/me", headers={"Authorization": f"Bearer {_token(sub=sub)}"})
    assert resp.status_code == 200
    assert resp.json()["sub"] == sub and resp.json()["role"] == "staff"


def test_scheme_is_case_insensitive(client: TestClient) -> None:
    assert client.get("/me", headers={"Authorization": f"bearer {_token()}"}).status_code == 200


@pytest.mark.parametrize(
    ("header", "reason"),
    [
        (None, "missing_token"),
        ("", "missing_token"),
        ("Basic abc", "bad_scheme"),
        ("Bearer", "bad_scheme"),
        ("Bearer a b", "bad_scheme"),
        ("Bearer not.a.jwt", "malformed"),
    ],
)
def test_bad_headers_are_401_with_reason(client: TestClient, header: str | None, reason: str) -> None:
    headers = {} if header is None else {"Authorization": header}
    resp = client.get("/me", headers=headers)
    assert resp.status_code == 401
    assert resp.headers["WWW-Authenticate"] == "Bearer"
    body = resp.json()["detail"]
    assert body["code"] == "CW-4010" and body["reason"] == reason


def test_wrong_secret_and_expired_are_401(client: TestClient) -> None:
    wrong = issue({"sub": "s", "tid": uuid4(), "role": "admin"}, secret="other")
    assert client.get("/me", headers={"Authorization": f"Bearer {wrong}"}).json()["detail"]["reason"] == (
        "bad_signature"
    )
    old = issue(
        {"sub": "s", "tid": uuid4(), "role": "admin"},
        secret=SECRET,
        clock=FrozenClock(datetime.now(tz=UTC) - timedelta(hours=1)),
    )
    assert (
        client.get("/me", headers={"Authorization": f"Bearer {old}"}).json()["detail"]["reason"] == "expired"
    )


def test_401_body_never_echoes_token(client: TestClient) -> None:
    token = _token()
    resp = client.get("/me", headers={"Authorization": f"Token {token}"})
    assert token not in resp.text


def test_jwt_secret_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(deps.JWT_SECRET_ENV, raising=False)
    with pytest.raises(RuntimeError, match=deps.JWT_SECRET_ENV):
        deps.jwt_secret()
    monkeypatch.setenv(deps.JWT_SECRET_ENV, "from-env")
    assert deps.jwt_secret() == "from-env"
