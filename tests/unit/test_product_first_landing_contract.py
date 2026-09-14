from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONSOLE = ROOT / "console" / "index.html"
README = ROOT / "README.md"


def test_product_first_landing_contract() -> None:
    html = CONSOLE.read_text(encoding="utf-8")
    readme = README.read_text(encoding="utf-8")

    for needle in (
        "실시간 상담 기록 · 근거 연결 · 사람 검토",
        ">새 상담 시작</button>",
        ">상담 기록 보기</button>",
        "합성 데이터로 기록 → 근거 확인 → 사람 검토 → 파기까지 전체 흐름을 안전하게 체험할 수 있습니다.",
    ):
        assert needle in html, needle

    for forbidden in (
        "Realtime clinical documentation backend",
        ">시스템 구조 보기</button>",
        "면접관에게",
    ):
        assert forbidden not in html, forbidden

    for needle in (
        "## 서비스 데모",
        "docs/images/demo.gif",
        "## 제품 흐름",
        "새 상담 → 실시간 기록 → 초안·근거 → 사람 검토 → 파기 → 파기 영수증",
    ):
        assert needle in readme, needle
