"""``GET /v1/release`` source order: explicit setting → Render's env → the literal ``unknown``."""

from __future__ import annotations

from chartwire.api.routers import release
from chartwire.core.config import Settings


def test_resolve_git_sha_prefers_the_explicit_setting() -> None:
    assert release.resolve_git_sha(Settings(git_sha="abc123"), {"RENDER_GIT_COMMIT": "def456"}) == "abc123"


def test_resolve_git_sha_falls_back_to_render_then_unknown() -> None:
    assert release.resolve_git_sha(Settings(git_sha=""), {"RENDER_GIT_COMMIT": " def456 "}) == "def456"
    assert release.resolve_git_sha(Settings(git_sha="   "), {"RENDER_GIT_COMMIT": ""}) == release.UNKNOWN
    assert release.resolve_git_sha(Settings(git_sha=""), {}) == release.UNKNOWN
