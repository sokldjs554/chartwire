"""Application errors rendered as RFC 9457 ``application/problem+json``."""

from __future__ import annotations

import re
from http import HTTPStatus
from typing import Any

_CODE_RE = re.compile(r"^CW-[45]\d{3}$")
PROBLEM_TYPE_PREFIX = "urn:chartwire:error:"


class AppError(Exception):
    """Domain error carrying a stable code (``CW-4xxx`` client, ``CW-5xxx`` server).

    ``retryable`` tells a client (REST or WebSocket) that the same request may succeed
    later, e.g. a Redis outage during ingest (``CW-5503``).
    """

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


class NotFound(AppError):
    def __init__(self, resource: str, code: str = "CW-4040") -> None:
        super().__init__(code, 404, f"{resource}을(를) 찾을 수 없습니다")


class Forbidden(AppError):
    def __init__(self, detail: str = "권한이 없습니다", code: str = "CW-4030") -> None:
        super().__init__(code, 403, detail)


class Conflict(AppError):
    def __init__(self, detail: str, code: str = "CW-4090") -> None:
        super().__init__(code, 409, detail)
