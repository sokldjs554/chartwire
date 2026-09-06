"""결정적 PII 리댁션 (spec §10.2) — 전화번호 · 도로명 주소 · 사람 이름.

두 곳이 이 모듈을 쓴다.

* **`segment_search.text`** — 평문 검색 색인. 스펙이 `[이름]` · `[전화]` · `[주소]` 토큰을 요구한다
  (§10.2). 암호문 `transcript_segments.text_enc` 는 원문을 그대로 보관하므로 임상의는 전사에서
  원문을 보고, 테넌트 전체를 훑는 색인만 가려진다.
* **SOAP 초안** — `notes/extractive.py` 가 식별자가 든 절을 문장과 근거에서 뺀다. 노트는 사람이
  그대로 읽는 문서이고 근거 인용은 저장된 발화의 부분 문자열이어야 하므로(검증기 규칙 2),
  거기서는 치환이 아니라 제외가 답이다.

**규칙이지 개체명 인식기가 아니다.** 이름은 성씨 사전에 있는 첫 음절 + 1~2음절 + 호칭(`님` /
`선생님`)일 때만 이름으로 본다. 호칭 없이 나오는 이름(``철수가 그랬어요``)은 잡지 못하고,
성씨가 아닌 글자로 시작하는 이름도 놓친다 — 그 한계는 `docs/limitations.md` 에 적어 두었다.
합성 말뭉치의 PII 는 `synth/scripts.py` 의 `_inject_pii` 가 넣고, `synth/gold.py` 의 `redact()`
가 기대값을 만든다. 두 결과가 같은지는 `tests/unit/test_core_pii.py` 가 20개 대본 전체로 확인한다.
"""

from __future__ import annotations

import re
from typing import Final

TOKENS: Final[dict[str, str]] = {"name": "[이름]", "phone": "[전화]", "address": "[주소]"}

# ``\b`` 는 쓰지 않는다: 파이썬 정규식에서 한글은 단어 문자라 ``…1491예요`` 의 숫자와 ``예`` 사이에
# 경계가 없어 매치가 통째로 실패한다. 숫자 경계는 숫자 lookaround 로 직접 쓴다.
PHONE_RE: Final = re.compile(r"(?<!\d)0\d{1,2}-\d{3,4}-\d{4}(?!\d)|(?<!\d)01[016-9]\d{7,8}(?!\d)")
"""``010-1234-5678`` 과 하이픈 없는 ``01012345678``. 진료 기록의 다른 숫자(용량·기간)와 겹치지 않는다."""

ADDRESS_RE: Final = re.compile(
    r"[가-힣]{1,8}(?:시|도)\s+[가-힣]{1,8}(?:구|군|읍|면)\s+[가-힣]{1,10}(?:로|길)\s*\d+(?:번길)?(?:\s*\d+)?"
)
"""``가온시 라온구 새벽로 13번길 17`` — 시/도 → 구/군/읍/면 → 로/길 + 번지가 모두 있을 때만."""

# 한국 성씨(빈도 상위). 합성 말뭉치의 `vocab_ko.SURNAMES` 50개를 포함한다.
SURNAMES: Final[frozenset[str]] = frozenset((
    "김", "이", "박", "최", "정", "강", "조", "윤", "장", "임", "한", "오", "서", "신", "권", "황", "안", "송", "류", "전",
    "홍", "고", "문", "양", "손", "배", "백", "허", "유", "남", "심", "노", "하", "곽", "성", "차", "주", "우", "구", "민",
    "진", "지", "엄", "채", "원", "천", "방", "공", "현", "함", "변", "염", "여", "추", "도", "소", "석", "선", "설", "마",
    "길", "연", "위", "표", "명", "기", "반", "왕", "금", "옥", "육", "인", "맹", "제", "모", "남궁", "탁", "국", "어", "은",
    "편", "용", "예", "봉", "한", "태", "갈", "온",
))  # fmt: skip
TWO_SYLLABLE_SURNAMES: Final[frozenset[str]] = frozenset(
    ("남궁", "선우", "황보", "제갈", "사공", "서문", "독고", "동방")
)
# 호칭 앞의 3~4음절만 후보다. 한국 성명은 대부분 성 1 + 이름 2 = 3음절이고, ``사장님``·``이모님`` 같은
# 직함·호칭은 2음절이라 이 길이 조건 하나가 오탐의 대부분을 걷어낸다. 대가는 2음절 이름을 놓치는 것이다.
# ``님`` 뒤의 조사(``님도``·``님께서``)는 그대로 두어야 하므로 뒤쪽 경계는 두지 않는다.
_NAME_CANDIDATE: Final = re.compile(r"(?<![가-힣])([가-힣]{3,4})(?=\s?(?:선생)?님)")
_NOT_NAMES: Final[frozenset[str]] = frozenset(("주치의", "담당의", "정신과", "소아과", "어르신", "사돈어른"))


def _is_name(token: str) -> bool:
    """성씨 + 이름 2음절(또는 복성 + 2음절)인가. 직함·호칭 명사는 제외한다."""
    if token in _NOT_NAMES:
        return False
    if len(token) == 4:
        return token[:2] in TWO_SYLLABLE_SURNAMES
    return token[0] in SURNAMES


def find_names(text: str) -> list[str]:
    return [m.group(1) for m in _NAME_CANDIDATE.finditer(text) if _is_name(m.group(1))]


def has_identifier(text: str) -> bool:
    """전화번호 · 도로명 주소 · 이름이 하나라도 있는가."""
    return bool(PHONE_RE.search(text) or ADDRESS_RE.search(text)) or bool(find_names(text))


def redact(text: str) -> str:
    """``[이름]`` · ``[전화]`` · ``[주소]`` 로 치환한 문자열 (`segment_search.text` 용)."""
    text = PHONE_RE.sub(TOKENS["phone"], text)
    text = ADDRESS_RE.sub(TOKENS["address"], text)
    return _NAME_CANDIDATE.sub(lambda m: TOKENS["name"] if _is_name(m.group(1)) else m.group(0), text)
