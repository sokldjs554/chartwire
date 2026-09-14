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


def _phone_media_block() -> str:
    """콘솔의 ``@media(max-width:720px){...}`` 블록 본문."""
    html = CONSOLE.read_text(encoding="utf-8")
    start = html.index("@media(max-width:720px){")
    depth, i = 0, html.index("{", start)
    for j in range(i, len(html)):
        if html[j] == "{":
            depth += 1
        elif html[j] == "}":
            depth -= 1
            if depth == 0:
                return html[i + 1 : j]
    raise AssertionError("phone media query is not balanced")


def test_phone_keeps_a_way_to_every_page() -> None:
    """폰에서 사이드바를 그냥 숨기면 '데이터 관리'(파기 영수증)에 갈 방법이 0개가 된다.

    홈에서 나가는 링크는 records·engineering 둘뿐이라, 사이드바가 유일한 통로다.
    숨기는 대신 가로 스트립으로 눕히고, 아이콘만 남지 않게 라벨을 되살린다.
    """
    block = _phone_media_block()
    assert ".sidebar{display:none}" not in block, "폰에서 사이드바를 숨기면 도달 불가 페이지가 생긴다"
    assert ".nav button span{display:inline}" in block, "폰에서는 아이콘만으로 구분할 수 없다"


def test_synthetic_notice_is_not_hidden_at_any_width() -> None:
    """합성 데이터 고지는 화면 폭에 따라 사라지면 안 된다 — 그래서 사이드바 밖 고정 띠에 둔다."""
    html = CONSOLE.read_text(encoding="utf-8")
    bar = html[html.index('class="safety-bar"') : html.index("</div>", html.index('class="safety-bar"'))]
    for needed in ("합성(SYNTHETIC)", "진단·치료 자동화 없음", "임상 사용 불가"):
        assert needed in bar, needed
    assert ".safety-bar{position:fixed" in html, "고지 띠는 스크롤·폭과 무관하게 붙어 있어야 한다"
