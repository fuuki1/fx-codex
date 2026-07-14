"""Daemon exit-code semantics that control launchd restart behavior."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from data_platform.collect.reconnect import ConnectionState
from tools.fx_quote_collector import EX_UNAVAILABLE, main as daemon_main


def test_exhausted_reconnect_budget_is_transient_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FX_OANDA_API_TOKEN", "secret")
    monkeypatch.setenv("FX_OANDA_ACCOUNT_ID", "account")
    monkeypatch.setenv("FX_OANDA_ENV", "practice")
    state = ConnectionState()
    state.stop("max_reconnects_exhausted")
    monkeypatch.setattr(
        "tools.fx_quote_collector.stream_quotes",
        lambda *_args, **_kwargs: (state, []),
    )

    code = daemon_main(["--output-root", str(tmp_path)])

    assert code == EX_UNAVAILABLE
    terminal = json.loads((tmp_path / "state" / "last_run.json").read_text())
    assert terminal["status"] == "source_unavailable"
    assert terminal["exit_code"] == EX_UNAVAILABLE
    incidents = list((tmp_path / "state" / "incidents").glob("collector_source_unavailable_*.json"))
    assert len(incidents) == 1
    incident = json.loads(incidents[0].read_text())
    assert incident["severity"] == "critical"
