from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CONSOLE = ROOT / "console" / "index.html"


def test_console_remains_single_file_and_has_no_nested_html_comments() -> None:
    html = CONSOLE.read_text(encoding="utf-8")
    assert html.lstrip().startswith("<!DOCTYPE html>")
    assert '<html lang="ko">' in html
    assert re.search(r"<script[^>]*\bsrc=", html) is None
    assert re.search(r"<link[^>]*\bhref=", html) is None
    comments = re.findall(r"<!--(.*?)-->", html, flags=re.S)
    assert all("<!--" not in body for body in comments)


def test_console_media_whitelist_matches_committed_images() -> None:
    from chartwire.api.app import CONSOLE_MEDIA

    for name, (source, mime) in CONSOLE_MEDIA.items():
        assert (ROOT / "docs" / "images" / source).is_file(), name
        assert mime in ("image/png", "image/gif"), (name, mime)


@pytest.mark.parametrize(
    "name",
    ["../../README.md", "..%2f..%2fREADME.md", "schema.sql", "demo.gif.bak", "", "00_intro.PNG"],
)
def test_console_media_rejects_outside_whitelist(name: str) -> None:
    from chartwire.api.app import CONSOLE_MEDIA

    assert name not in CONSOLE_MEDIA


def test_console_media_resolves_from_container_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from chartwire.api import app as app_module

    monkeypatch.setattr(
        app_module.Path,
        "resolve",
        lambda self: tmp_path / "site-packages" / "x" / "y" / "z" / "app.py",
    )
    console = tmp_path / "app" / "console"
    (console / "media").mkdir(parents=True)
    (console / "index.html").write_text("<!DOCTYPE html>", encoding="utf-8")
    (console / "media" / "00_intro.png").write_bytes(b"\x89PNG")
    found = app_module.console_media_files(console / "index.html")
    assert set(found) == {"00_intro.png"}
    assert found["00_intro.png"] == (console / "media" / "00_intro.png", "image/png")
    assert app_module.console_media_files(None) == {}
