"""Contract checks for the redesigned ChartWire product console.

The redesign deliberately hides protocol/ops detail from the default consultation UI,
but the real API/WebSocket contract must remain present and executable.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CONSOLE = ROOT / "console" / "index.html"


def _script() -> str:
    html = CONSOLE.read_text(encoding="utf-8")
    scripts = re.findall(r"<script>(.*?)</script>", html, flags=re.S)
    assert len(scripts) == 1
    return scripts[0]


def test_product_information_architecture_is_separated() -> None:
    html = CONSOLE.read_text(encoding="utf-8")
    assert html.lstrip().startswith("<!DOCTYPE html>")
    for page in (
        "page-home",
        "page-session",
        "page-records",
        "page-review",
        "page-data",
        "page-engineering",
    ):
        assert f'id="{page}"' in html
    assert "상담에 집중하세요." in html
    assert "연결 상세 보기" in html
    assert "시스템 상세" in html
    assert "데이터 관리" in html
    assert "모든 데이터는 합성(SYNTHETIC)입니다 — 실제 환자 정보 없음" in html
    assert "네트워크 끊기 5초" in html
    assert "AI가 작성하지 않는 영역" in html
    assert "prefers-color-scheme" in html


def test_backend_contract_is_still_reachable() -> None:
    text = CONSOLE.read_text(encoding="utf-8")
    for needle in (
        "/v1/auth/token",
        "/v1/sessions?limit=",
        "/console/scripts.json",
        "/ws-ticket",
        "/ws/v1/ingest",
        "/ws/v1/watch",
        "/notes/latest",
        "/notes/draft",
        "/assessment",
        "/sign",
        "/revoke",
        "/v1/purge-jobs",
        "/verify-decrypt",
        "/v1/ops/dead-letters",
        "/metrics",
        "last_sent_seq",
        "risk.alert",
        "risk.ack",
    ):
        assert needle in text, needle


def test_protocol_constants_and_recovery_path_remain_in_source() -> None:
    script = _script()
    for needle in (
        "0x4357",
        "FLAG_SIM:4",
        "HEADER:12",
        "CHUNK_MS:200",
        "CHUNK_BYTES:6400",
        "welcome",
        "ack_seq",
        "credit",
        "missing",
        "nack",
        "pong",
        "RESUME OK",
        "4409",
        "4503",
        "1012",
    ):
        assert needle in script, needle


@pytest.fixture(scope="module")
def node() -> str:
    exe = shutil.which("node")
    if exe is None:
        pytest.skip("node is not installed")
    return exe


def test_console_script_passes_node_check(node: str, tmp_path: Path) -> None:
    js = tmp_path / "console.js"
    js.write_text(_script(), encoding="utf-8")
    proc = subprocess.run(
        [node, "--check", str(js)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr


def test_no_duplicate_ids_or_dead_navigation_targets() -> None:
    html = CONSOLE.read_text(encoding="utf-8")
    ids = re.findall(r'id="([^"]+)"', html)
    assert len(ids) == len(set(ids))
    pages = set(re.findall(r'id="page-([^"]+)"', html))
    targets = set(re.findall(r'data-(?:go|page)="([^"]+)"', html))
    assert targets <= pages
