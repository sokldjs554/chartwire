"""structlog JSON logging with a PHI redaction processor (spec §0.9, §3.1).

Nothing that could carry transcript text, names, phone numbers, tokens or key material
may reach a log line. ``redact_phi`` is applied to every event dict before rendering.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from typing import Any

import structlog
from structlog.contextvars import bind_contextvars, clear_contextvars

REDACTED = "[REDACTED]"
PHI_KEYS: frozenset[str] = frozenset({"text", "quote", "name", "phone", "token", "ticket", "dek", "payload"})
_PHI_PATTERN = re.compile(r"\d{2,4}-\d{3,4}-\d{4}|\d{6}-\d{7}")
"""전화번호(휴대폰 010-…, 지역번호 02-…/031-…, 대표번호 1588-…) / 주민등록번호. §3.1의
``\\d{3}-\\d{3,4}-\\d{4}``를 포함하는 상위 집합 — 서울 지역번호(2자리)를 놓치지 않기 위해 넓혔다."""
_UUID_PATTERN = re.compile(r"\A[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\Z")
"""식별자는 PHI 가 아니다. UUID 안의 숫자 그룹(예: ``…-4051-9308-…``)이 전화번호 패턴과 우연히 겹쳐
``session_id`` 가 통째로 가려지는 오탐을 막는다 — 부하 로그에서 실제로 관측됐다(loadfix §3)."""


def redact_value(value: Any) -> Any:
    """Redact a single value: strings matching PHI patterns; nested containers recursively."""
    if isinstance(value, str):
        if _UUID_PATTERN.match(value):
            return value
        return REDACTED if _PHI_PATTERN.search(value) else value
    if isinstance(value, dict):
        return redact_dict(value)
    if isinstance(value, list | tuple):
        return type(value)(redact_value(v) for v in value)
    return value


def redact_dict(event_dict: dict[str, Any]) -> dict[str, Any]:
    """Return a copy with PHI keys replaced by ``[REDACTED]`` and pattern hits masked."""
    return {k: (REDACTED if k in PHI_KEYS else redact_value(v)) for k, v in event_dict.items()}


def redact_phi(_logger: Any, _method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """structlog processor form of :func:`redact_dict`."""
    return redact_dict(event_dict)


_RESERVED: frozenset[str] = frozenset(vars(logging.LogRecord("", 0, "", 0, "", (), None))) | frozenset(
    {"message", "asctime", "taskName"}
)
"""Attributes stdlib puts on every record; anything else came from ``extra=``."""


class ExtraFormatter(logging.Formatter):
    """Render ``log.warning("msg", extra={...})`` — a bare ``%(message)s`` drops the extras.

    Half of the runtime's diagnostics (``store path failed`` with its ``error``, ``session_id`` on
    every stt-worker line) live in ``extra``; losing them makes a production incident unreadable
    (this is exactly what happened to the first A n=200 load run — ``docs/dev/handoff/loadfix.md``).
    The extras go through :func:`redact_dict`, so the §0.9 PHI rule still holds.
    """

    def format(self, record: logging.LogRecord) -> str:
        extra = {k: v for k, v in record.__dict__.items() if k not in _RESERVED}
        if not extra:
            return super().format(record)
        rendered = json.dumps(redact_dict(extra), ensure_ascii=False, default=str, sort_keys=True)
        # A copy so the extras land before the traceback and the original record stays intact
        # (other handlers, and ``basicConfig`` re-entry, must still see the raw message).
        merged = logging.makeLogRecord(record.__dict__)
        merged.msg = f"{record.getMessage()} {rendered}"
        merged.args = ()
        return super().format(merged)


def configure(level: str = "INFO", *, json: bool = True) -> None:
    """Configure structlog + stdlib logging once per process."""
    renderer: Any = structlog.processors.JSONRenderer() if json else structlog.dev.ConsoleRenderer()
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            redact_phi,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level.upper())),
        logger_factory=structlog.PrintLoggerFactory(sys.stderr),
        cache_logger_on_first_use=True,
    )
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(ExtraFormatter("%(levelname)s %(name)s %(message)s"))
    logging.basicConfig(level=level.upper(), handlers=[handler])


def bind_context(
    *,
    request_id: str | None = None,
    tenant_id: Any = None,
    session_id: Any = None,
    node_id: str | None = None,
) -> None:
    """Bind the four correlation fields for the current task; ``None`` values are skipped."""
    values = {"request_id": request_id, "tenant_id": tenant_id, "session_id": session_id, "node_id": node_id}
    bind_contextvars(**{k: str(v) for k, v in values.items() if v is not None})


def clear_context() -> None:
    clear_contextvars()


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)  # type: ignore[no-any-return]
