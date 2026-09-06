"""``typer.testing`` 출력 비교용 헬퍼.

rich 는 색을 켜면 옵션 이름을 한 덩어리로 칠하지 않는다: ``--tenant`` 가
``\x1b[1;36m-\x1b[0m\x1b[1;36m-tenant\x1b[0m`` 로 나뉘어 리터럴 ``"--tenant"`` 가 출력 문자열에서 사라진다.
색을 켤지는 실행 환경이 정한다 — GitHub Actions 러너에서는 켜지고 파이프로 받는 로컬 셸에서는 꺼진다.
그래서 옵션 이름이 도움말에 있는지 보는 단언은 먼저 이스케이프를 벗겨야 두 곳에서 같은 답을 낸다.
"""

from __future__ import annotations

import re

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def plain(text: str) -> str:
    """ANSI 이스케이프를 제거한 사람이 읽는 텍스트."""
    return _ANSI.sub("", text)
