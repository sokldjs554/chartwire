"""``GET /v1/release`` — public, names the build, nothing else (the live gate's contract).

Role coverage (anonymous allowed) is also enforced by ``test_rbac_matrix.py`` through ``rbac.MATRIX``;
this file pins the body: the configured sha, the package version, the node id, and no extra fields.

Env: ``CHARTWIRE_TEST_DB=chartwire_test_e CHARTWIRE_TEST_REDIS_DB=5``.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from chartwire import __version__
from chartwire.objectstore.localfs import LocalFs
from tests.integration.api_support import build_app, client, make_deps

pytestmark = pytest.mark.integration


@pytest.fixture
def api_app(app_engine, redis, settings, tmp_path):
    return build_app(make_deps(app_engine, redis, settings, LocalFs(tmp_path)))


async def test_release_is_public_and_reports_the_configured_commit(api_app, settings, monkeypatch):
    monkeypatch.setattr(settings, "git_sha", "0123abcd" * 5)
    async with client(api_app) as c:
        resp = await c.get("/v1/release")  # no Authorization header on purpose
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {"git_sha", "version", "node_id", "started_at"}
    assert body["git_sha"] == "0123abcd" * 5
    assert body["version"] == __version__
    assert body["node_id"] == "test-e"
    datetime.fromisoformat(body["started_at"])  # ISO-8601, parseable


async def test_release_says_unknown_rather_than_guessing(api_app, settings, monkeypatch):
    monkeypatch.setattr(settings, "git_sha", "")
    monkeypatch.delenv("RENDER_GIT_COMMIT", raising=False)
    async with client(api_app) as c:
        resp = await c.get("/v1/release")
    assert resp.status_code == 200 and resp.json()["git_sha"] == "unknown"
