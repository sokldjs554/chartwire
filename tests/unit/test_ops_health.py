"""Health: livez always 200; readyz 503 on drain, failing/raising/slow checks; sync + async probes."""

from __future__ import annotations

import asyncio

import pytest

from chartwire.ops.health import ERROR, FAIL, OK, TIMEOUT, Health


async def test_ready_with_no_checks() -> None:
    h = Health(info={"role": "api", "node_id": "n1"})
    assert h.livez() == (200, {"status": OK, "draining": False, "role": "api", "node_id": "n1"})
    code, body = await h.readyz()
    assert code == 200
    assert body == {"status": "ready", "draining": False, "checks": {}, "role": "api", "node_id": "n1"}


async def test_draining_makes_readyz_503_but_livez_stays_200() -> None:
    h = Health()
    calls = 0

    def probe() -> bool:
        nonlocal calls
        calls += 1
        return True

    h.add_check("postgres", probe)
    h.mark_draining()
    assert h.draining is True
    assert h.livez()[0] == 200
    code, body = await h.readyz()
    assert code == 503
    assert body["status"] == "draining" and body["draining"] is True
    assert calls == 0, "checks are not run while draining"


async def test_sync_and_async_checks_reported_per_name() -> None:
    h = Health()
    h.add_check("postgres", lambda: True)

    async def redis_ok() -> bool:
        await asyncio.sleep(0)
        return True

    h.add_check("redis", redis_ok)
    assert h.check_names == ("postgres", "redis")
    code, body = await h.readyz()
    assert code == 200
    assert body["checks"] == {"postgres": OK, "redis": OK}


async def test_one_failing_check_flips_to_503_with_detail() -> None:
    h = Health()
    h.add_check("postgres", lambda: True)
    h.add_check("redis", lambda: False)
    code, body = await h.readyz()
    assert code == 503
    assert body["status"] == "unavailable"
    assert body["checks"] == {"postgres": OK, "redis": FAIL}


async def test_raising_and_slow_checks_are_failures_not_crashes() -> None:
    h = Health(check_timeout_s=0.02)

    def boom() -> bool:
        raise ConnectionError("down")

    async def slow() -> bool:
        await asyncio.sleep(1)
        return True

    h.add_check("postgres", boom)
    h.add_check("redis", slow)
    code, body = await h.readyz()
    assert code == 503
    assert body["checks"] == {"postgres": ERROR, "redis": TIMEOUT}


def test_validation() -> None:
    h = Health()
    h.add_check("x", lambda: True)
    with pytest.raises(ValueError, match="already registered"):
        h.add_check("x", lambda: True)
    with pytest.raises(ValueError, match="non-empty"):
        h.add_check("", lambda: True)
    with pytest.raises(ValueError, match="positive"):
        Health(check_timeout_s=0)
