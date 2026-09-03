"""``chartwire serve`` argument handling and its import guards (§13.1): an unknown role and a missing
work-package module both fail fast with a clear message and exit code 2 — never a traceback. Plus the
worker's guarded handler-module loading (a missing module is skipped and logged; a broken one raises).
"""

from __future__ import annotations

import sys

import pytest
from typer.testing import CliRunner

from chartwire.worker import cli
from chartwire.worker import main as worker_main

runner = CliRunner()


def test_unknown_role_exits_2() -> None:
    result = runner.invoke(cli.app, ["bogus"])
    assert result.exit_code == 2
    assert "api, worker, stt-worker, all" in result.output


@pytest.mark.parametrize(
    ("role", "module", "wp"),
    [("api", "chartwire.api.app", "WP-E"), ("stt-worker", "chartwire.stt.worker", "WP-C")],
)
def test_missing_module_is_reported_not_raised(monkeypatch, role: str, module: str, wp: str) -> None:
    monkeypatch.setitem(sys.modules, module, None)  # import → ModuleNotFoundError, whatever the tree has
    result = runner.invoke(cli.app, [role])
    assert result.exit_code == 2
    assert module in result.output and wp in result.output


def test_worker_role_dispatches_to_worker_main(monkeypatch) -> None:
    seen: list[str] = []
    monkeypatch.setattr(cli, "configure", lambda level: None)  # keep the test process's logging untouched
    monkeypatch.setattr(cli.worker_main, "main", lambda settings: seen.append(settings.node_id) or 0)
    result = runner.invoke(cli.app, ["worker", "--log-level", "WARNING"])
    assert result.exit_code == 0 and len(seen) == 1


def test_root_cli_mounts_serve_and_outbox() -> None:
    from chartwire.cli import MOUNTED

    assert {"serve", "outbox"} <= set(MOUNTED)


def test_load_handlers_skips_missing_modules_and_logs_without_reserved_fields(caplog) -> None:
    caplog.set_level("INFO", logger="chartwire.worker.main")
    loaded = worker_main.load_handlers(("outbox_prune", "not_written_yet"))
    assert loaded["outbox_prune"] is not None and loaded["not_written_yet"] is None
    assert any(r.handler_module == "not_written_yet" for r in caplog.records)  # type: ignore[attr-defined]


def test_load_handlers_reraises_import_errors_inside_a_present_module(monkeypatch) -> None:
    import importlib

    def broken(path: str):
        raise ModuleNotFoundError("No module named 'boto3'", name="boto3")

    monkeypatch.setattr(importlib, "import_module", broken)
    with pytest.raises(ModuleNotFoundError):
        worker_main.load_handlers(("purge_run",))
