"""``GET /v1/release`` — which build is answering (public, ``rbac.PUBLIC``).

The live gate (``.github/workflows/live-gate.yml`` → ``scripts/wait_for_release.py``) needs to know
*which* build answered before it trusts anything else it sees on the public URL: for minutes after a
push the previous release still serves, and a smoke that starts then verifies the old commit and
reports it as this one. So the gate waits until ``git_sha`` here equals the commit that triggered it,
and only then runs its checks.

Sources, first match wins: ``CHARTWIRE_GIT_SHA`` (set explicitly — CI passes it to ``docker run``),
``RENDER_GIT_COMMIT`` (Render sets it on every deploy), otherwise the literal ``"unknown"`` — never a
guess from the working tree, which the container does not have anyway. Nothing here is secret: the
commit id of a public repository, the package version, the node id already in every response header,
and when this process started.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import UTC, datetime

from fastapi import APIRouter, Request

from chartwire import __version__
from chartwire.api.deps import get_deps
from chartwire.api.schemas import ReleaseOut
from chartwire.core.config import Settings

router = APIRouter(prefix="/v1", tags=["release"])

STARTED_AT = datetime.now(UTC)
"""Import time ≈ process start; the gate reads it to tell a fresh container from a woken one."""

UNKNOWN = "unknown"


def resolve_git_sha(settings: Settings, environ: Mapping[str, str] = os.environ) -> str:
    """``CHARTWIRE_GIT_SHA`` → ``RENDER_GIT_COMMIT`` → ``"unknown"``; whitespace never counts as a value."""
    for value in (settings.git_sha, environ.get("RENDER_GIT_COMMIT", "")):
        if value and value.strip():
            return value.strip()
    return UNKNOWN


@router.get("/release", response_model=ReleaseOut)
async def release(request: Request) -> ReleaseOut:
    settings: Settings = request.app.state.settings
    return ReleaseOut(
        git_sha=resolve_git_sha(settings),
        version=__version__,
        node_id=get_deps(request).node_id,  # the same id every response header carries
        started_at=STARTED_AT,
    )
