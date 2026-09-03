"""structlog JSON logging with a PHI redaction processor (spec §0.9, §3.1).

Nothing that could carry transcript text, names, phone numbers, tokens or key material
may reach a log line. ``redact_phi`` is applied to every event dict before rendering.
"""

from __future__ import annotations

import logging
import re
import sys
from typing import Any

import structlog
from structlog.contextvars import bind_contextvars, clear_contextvars

REDACTED = "[REDACTED]"
PHI_KEYS: frozenset[str] = frozenset({"text", "quote", "name", "phone", "token", "ticket", "dek", "payload"})
_PHI_PATTERN = re.compile(r"\d{3}-\d{3,4}-\d{4}|\d{6}-\d{7}")  # 전화번호 / 주민등록번호


def redact_value(value: Any) -> Any:
    """Redact a single value: strings matching PHI patterns; nested containers recursively."""
    if isinstance(value, str):
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
    logging.basicConfig(level=level.upper(), stream=sys.stderr, format="%(message)s")


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
