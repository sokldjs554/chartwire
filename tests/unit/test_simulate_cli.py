"""``chartwire simulate`` argument helpers (§13.1): it is mounted as its own sub-app (separate from
``loadtest``), the chunk count follows the script length, and ``--drop-at`` accepts ``30s``-style values."""

from __future__ import annotations

import pytest
import typer
from typer.testing import CliRunner

from chartwire.loadtest import simulate_cli

from ._cli_output import plain

runner = CliRunner()


def test_root_cli_mounts_simulate_separately_from_loadtest() -> None:
    from chartwire.cli import MOUNTED

    assert "simulate" in MOUNTED


def test_help_lists_the_spec_options() -> None:
    result = runner.invoke(simulate_cli.app, ["--help"])
    assert result.exit_code == 0
    helptext = plain(result.output)
    for option in ("--script", "--speed", "--session", "--drop-at"):
        assert option in helptext


@pytest.mark.parametrize(
    ("value", "expected"), [("30s", 30.0), ("1500ms", 1.5), ("2m", 120.0), ("7", 7.0), (" 0.5 s", 0.5)]
)
def test_parse_duration(value: str, expected: float) -> None:
    assert simulate_cli.parse_duration_s(value) == pytest.approx(expected)


def test_parse_duration_rejects_garbage() -> None:
    with pytest.raises(typer.BadParameter):
        simulate_cli.parse_duration_s("soon")


def test_total_chunks_covers_the_script_tail() -> None:
    assert simulate_cli.total_chunks(166_639, 200) == 834  # 833 full chunks + the tail
    assert simulate_cli.total_chunks(200, 200) == 1
    assert simulate_cli.total_chunks(1, 200) == 1


def test_ws_url_from_api_base() -> None:
    assert simulate_cli.ws_url("http://127.0.0.1:8000", "ingest") == "ws://127.0.0.1:8000/ws/v1/ingest"
    assert simulate_cli.ws_url("https://demo.example/", "watch") == "wss://demo.example/ws/v1/watch"
