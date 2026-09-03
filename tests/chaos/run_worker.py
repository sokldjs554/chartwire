"""Subprocess entry for ``test_worker_sigkill.py``: a real worker (poller + drain) with the chaos handler.

Reads ``CHARTWIRE_*`` from the environment (the test points them at its own database / Redis index).
No HTTP port, no ticker modules — only the poller, so the process is exactly the code under test.
"""

from __future__ import annotations

import asyncio
import sys

from chartwire.core.config import get_settings
from chartwire.core.logging import configure
from chartwire.outbox.registry import REGISTRY
from chartwire.worker import main as worker_main
from tests.chaos import chaos_handler


def main() -> int:
    configure("INFO")
    chaos_handler.register(REGISTRY)
    return asyncio.run(
        worker_main.run(
            get_settings(),
            http_port=0,
            handler_modules=(),
            poller_kwargs={"tick_s": 0.2, "reclaim_every_s": 0.5, "stats_every_s": 1e9},
        )
    )


if __name__ == "__main__":
    sys.exit(main())
