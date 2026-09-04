"""``rls.json`` aggregator (spec §11.1): route × role × tenant cross-access attempts and leaks.

The attempts are made by the RBAC/RLS matrix test over the live API (WP-E); that test writes its
tally to ``var/eval/rls.json`` (``{"attempts": N, "leaks": 0, "routes": N, "roles": 5}``). This
module only validates and re-emits it under the common header — see :mod:`purge_eval`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final

from chartwire.eval.purge_eval import collect as _collect

DEFAULT_INPUT: Final = Path("var/eval/rls.json")
REQUIRED: Final[tuple[str, ...]] = ("attempts", "leaks")


def collect(source: Path = DEFAULT_INPUT) -> dict[str, Any]:
    return _collect(source, required=REQUIRED)
