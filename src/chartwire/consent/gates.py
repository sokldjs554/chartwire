"""Deterministic consent gates (§8.3, ADR-0003). No I/O: callers pass consent rows in.

``active_scopes`` implements "latest non-revoked row": the row with the highest
``version`` governs, and if *that* row is revoked the patient has withdrawn
consent — nothing is active, even if an older version was never explicitly
revoked. Any other reading would let a superseded consent resurrect scopes
after a revocation, i.e. fail open.

Every gate raises :class:`ConsentScopeMissing`, which carries both wire codes
the spec fixes: REST ``403 CW-4031 CONSENT_SCOPE_MISSING`` and WebSocket close
``4011``. ``consent/service.py`` (Phase 1) loads the rows and calls these functions.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Final, Protocol

from chartwire.consent.scopes import SCOPES

CODE: Final = "CW-4031"
WS_CODE: Final = 4011
STATUS: Final = 403


class ConsentRow(Protocol):
    """Structural view of a ``consents`` row (SQLAlchemy model, dataclass or test stub)."""

    @property
    def version(self) -> int: ...

    @property
    def scopes(self) -> Sequence[str]: ...

    @property
    def revoked_at(self) -> datetime | None: ...


class ConsentScopeMissing(Exception):
    """The live consent does not include ``scope``. Mapped to problem+json / WS close by the shells."""

    code: Final = CODE
    ws_code: Final = WS_CODE
    status: Final = STATUS
    retryable: Final = False

    def __init__(self, scope: str) -> None:
        self.scope = scope
        self.detail = f"동의 범위 '{scope}'가 없어 처리할 수 없습니다 (CONSENT_SCOPE_MISSING)."
        super().__init__(self.detail)


def _check_scope_name(scope: str) -> None:
    if scope not in SCOPES:
        raise ValueError(f"unknown consent scope: {scope}")


def active_scopes(consent_rows: Iterable[ConsentRow]) -> set[str]:
    """Scopes granted by the latest consent version, or ``set()`` when it is revoked / absent."""
    latest: ConsentRow | None = None
    for row in consent_rows:
        if latest is None or row.version > latest.version:
            latest = row
    if latest is None or latest.revoked_at is not None:
        return set()
    return set(latest.scopes) & SCOPES


def has_scope(scopes: Iterable[str], scope: str) -> bool:
    _check_scope_name(scope)
    return scope in set(scopes)


def require_scope(scopes: Iterable[str], scope: str) -> None:
    """Raise :class:`ConsentScopeMissing` unless ``scope`` is in ``scopes`` (an unknown name is a bug → ValueError)."""
    if not has_scope(scopes, scope):
        raise ConsentScopeMissing(scope)
