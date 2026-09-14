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
README = ROOT / "README.md"


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


def test_evaluator_guidance_lives_in_readme_not_product_ui() -> None:
    html = CONSOLE.read_text(encoding="utf-8")
    readme = README.read_text(encoding="utf-8")

    for forbidden in (
        "60 sec recommended demo",
        "면접관에게는 이 흐름만 보여주세요",
        'id="guidedDemoStart"',
        'class="demo-journey"',
        "data-tour-page",
    ):
        assert forbidden not in html, forbidden

    for needle in (
        "visual-hierarchy-v2",
        "파기 영수증 확인",
        ".feature:nth-child(4):before",
    ):
        assert needle in html, needle

    for needle in (
        "상담에 집중하세요. ChartWire는 실시간 기록부터 근거 연결 초안, 사람 검토, 데이터 파기 증적까지 한 흐름으로 연결합니다.",
        "## 서비스 데모",
        "docs/images/demo.gif",
        "## 제품 흐름",
        "새 상담 → 실시간 기록 → 초안·근거 → 사람 검토 → 파기 → 파기 영수증",
    ):
        assert needle in readme, needle


def test_product_first_landing_stays_service_like() -> None:
    html = CONSOLE.read_text(encoding="utf-8")
    for needle in (
        "consult-preview-v1",
        "상담 기록 중",
        "근거 연결됨 ✓",
        ">새 상담 시작</button>",
        "N=100 ACK p95",
        "검증된 전송 결과",
        # 값(449)이 아니라 출처를 단언한다 — 측정이 갱신되면 값은 바뀌어야 하고,
        # 손으로 적힌 숫자가 다시 들어오면 이 단언이 깨져야 한다.
        "<!-- num:load.A.n100.ack_p95_ms -->",
        "<!-- num:load.A.n100.chunks_per_s -->",
        "<!-- num:load.A.n100.loss -->",
    ):
        assert needle in html, needle
    for forbidden in (
        "새 합성 상담 시작",
        "60초 데모 시작",
        "면접관에게",
        "Realtime clinical documentation backend",
        ">시스템 구조 보기</button>",
    ):
        assert forbidden not in html, forbidden

    for needle in (
        "실시간 상담 기록 · 근거 연결 · 사람 검토",
        ">상담 기록 보기</button>",
        "합성 데이터로 기록 → 근거 확인 → 사람 검토 → 파기까지 전체 흐름을 안전하게 체험할 수 있습니다.",
    ):
        assert needle in html, needle


def test_deep_demo_features_are_restored_inside_product_ui() -> None:
    html = CONSOLE.read_text(encoding="utf-8")
    for needle in (
        'data-depth-restored="v5"',
        'id="scriptSelect"',
        'value="staff"',
        'id="riskSla"',
        'id="purgeSteps"',
        'id="purgeJobInput"',
        'id="purgeLog"',
        'id="opsAuto"',
        'href="/docs"',
        "renderDetailedReceipt",
        "receipt_hash_valid",
        "재계산 해시 일치",
        "원본 JSON 보기",
    ):
        assert needle in html, needle


def test_receipt_keeps_receipt_like_paper_presentation() -> None:
    html = CONSOLE.read_text(encoding="utf-8")
    for needle in (
        "receipt-paper-v2",
        "#receiptBox.receipt.paper",
        "VERIFIED AUDIT EVIDENCE · SYNTHETIC DEMO",
        "CHARTWIRE",
    ):
        assert needle in html, needle


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
