"""Liveness / readiness state for ``/healthz`` and ``/readyz`` (§6.4 step 7, §7.3, §12).

``/healthz`` answers "is the process alive" and is always 200 (the container HEALTHCHECK must not kill
a draining process). ``/readyz`` answers "may the load balancer send traffic": 503 while draining or
while any registered dependency check fails, otherwise 200 with per-check detail.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

log = logging.getLogger(__name__)

Check = Callable[[], bool | Awaitable[bool]]
"""Dependency probe: sync or async, truthy = healthy. Exceptions and timeouts count as failures."""

DEFAULT_CHECK_TIMEOUT_S = 2.0

OK = "ok"
FAIL = "fail"
TIMEOUT = "timeout"
ERROR = "error"

STATUS_READY = "ready"
STATUS_DRAINING = "draining"
STATUS_UNAVAILABLE = "unavailable"


class Health:
    def __init__(
        self,
        *,
        check_timeout_s: float = DEFAULT_CHECK_TIMEOUT_S,
        info: Mapping[str, str] | None = None,
    ) -> None:
        if check_timeout_s <= 0:
            raise ValueError("check_timeout_s must be positive")
        self._timeout_s = check_timeout_s
        self._info: dict[str, str] = dict(info or {})
        self._checks: dict[str, Check] = {}
        self._draining = False

    # --- state --------------------------------------------------------------------------------------
    @property
    def draining(self) -> bool:
        return self._draining

    def mark_draining(self) -> None:
        """Flip readiness to 503; wired to ``Drainer.on_begin``."""
        self._draining = True

    def add_check(self, name: str, fn: Check) -> None:
        if not name:
            raise ValueError("check name must be non-empty")
        if name in self._checks:
            raise ValueError(f"check {name!r} already registered")
        self._checks[name] = fn

    @property
    def check_names(self) -> tuple[str, ...]:
        return tuple(self._checks)

    # --- probes -------------------------------------------------------------------------------------
    def livez(self) -> tuple[int, dict[str, Any]]:
        return 200, {"status": OK, "draining": self._draining, **self._info}

    async def readyz(self) -> tuple[int, dict[str, Any]]:
        if self._draining:
            return 503, {"status": STATUS_DRAINING, "draining": True, "checks": {}, **self._info}
        names = list(self._checks)
        results = await asyncio.gather(*(self._run(name, self._checks[name]) for name in names))
        checks = dict(zip(names, results, strict=True))
        healthy = all(result == OK for result in results)
        status = STATUS_READY if healthy else STATUS_UNAVAILABLE
        return (200 if healthy else 503), {
            "status": status,
            "draining": False,
            "checks": checks,
            **self._info,
        }

    async def _run(self, name: str, fn: Check) -> str:
        try:
            outcome = fn()
            if inspect.isawaitable(outcome):
                outcome = await asyncio.wait_for(outcome, timeout=self._timeout_s)
        except TimeoutError:
            log.warning("readiness check timed out", extra={"check": name})
            return TIMEOUT
        except Exception:
            log.warning("readiness check raised", extra={"check": name}, exc_info=True)
            return ERROR
        return OK if outcome else FAIL
