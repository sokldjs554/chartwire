"""Application errors rendered as RFC 9457 ``application/problem+json``."""

from __future__ import annotations

import re
from collections.abc import Mapping
from http import HTTPStatus
from typing import Any

_CODE_RE = re.compile(r"^CW-[45]\d{3}$")
PROBLEM_TYPE_PREFIX = "urn:chartwire:error:"


class AppError(Exception):
    """Domain error carrying a stable code (``CW-4xxx`` client, ``CW-5xxx`` server).

    ``retryable`` tells a client (REST or WebSocket) that the same request may succeed
    later, e.g. a Redis outage during ingest (``CW-5503``). ``headers`` are extra response
    headers the api's error handler adds to the problem response (``Retry-After``); the
    problem document itself never carries them.
    """

    headers: Mapping[str, str] | None = None

    def __init__(self, code: str, status: int, detail: str, retryable: bool = False) -> None:
        if not _CODE_RE.match(code):
            raise ValueError(f"invalid error code {code!r}; expected CW-4xxx or CW-5xxx")
        super().__init__(detail)
        self.code = code
        self.status = status
        self.detail = detail
        self.retryable = retryable

    def to_problem(self, request_id: str | None = None) -> dict[str, Any]:
        """RFC 9457 problem document; ``code``/``request_id``/``retryable`` are extension members."""
        return {
            "type": PROBLEM_TYPE_PREFIX + self.code,
            "title": HTTPStatus(self.status).phrase,
            "status": self.status,
            "detail": self.detail,
            "code": self.code,
            "request_id": request_id,
            "retryable": self.retryable,
        }

    def __repr__(self) -> str:
        return f"AppError({self.code}, {self.status}, {self.detail!r}, retryable={self.retryable})"


def object_particle(word: str) -> str:
    """한국어 목적격 조사: 받침이 있으면 ``을``, 없으면 ``를``.

    오류 detail 은 콘솔이 그대로 띄워 사람이 읽는 문장이라 ``노트을(를)`` 처럼 두지 않는다.
    한글이 아닌 끝글자(영문·숫자·UUID)는 읽는 법이 갈리므로 판정하지 않고 ``을(를)`` 로 남긴다.
    """
    if not word:
        return "을(를)"
    last = word[-1]
    if "가" <= last <= "힣":
        return "을" if (ord(last) - 0xAC00) % 28 else "를"
    return "을(를)"


class NotFound(AppError):
    def __init__(self, resource: str, code: str = "CW-4040") -> None:
        super().__init__(code, 404, f"{resource}{object_particle(resource)} 찾을 수 없습니다")


class Forbidden(AppError):
    def __init__(self, detail: str = "권한이 없습니다", code: str = "CW-4030") -> None:
        super().__init__(code, 403, detail)


class Conflict(AppError):
    def __init__(self, detail: str, code: str = "CW-4090") -> None:
        super().__init__(code, 409, detail)


class RateLimited(AppError):
    """429 with ``Retry-After`` (seconds until the fixed window rolls over); always retryable."""

    def __init__(self, detail: str, retry_after_s: int, code: str = "CW-4290") -> None:
        super().__init__(code, 429, detail, retryable=True)
        self.retry_after_s = retry_after_s
        self.headers = {"Retry-After": str(max(1, retry_after_s))}
