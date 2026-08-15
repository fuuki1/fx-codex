from __future__ import annotations

from datetime import UTC, datetime, timedelta
import fcntl
import json
import os
import warnings

import pytest

from fx_intel import stage_timing
from fx_intel.stage_timing import CONTRACT, StageTimingRecorder, summarize

START = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)


def test_records_utc_and_monotonic_stage_timing(tmp_path) -> None:
    timestamps = iter((START, START + timedelta(seconds=10)))
    monotonic = iter((1_000_000_000, 1_125_500_000))
    target = tmp_path / "stages.jsonl"
    recorder = StageTimingRecorder(
        target,
        pipeline="briefing",
        trace_id="trace-1",
        clock=lambda: next(timestamps),
        monotonic_ns=lambda: next(monotonic),
        metadata={"mode": "per_timeframe"},
    )

    with recorder.stage("decision_log_read", rows=12):
        recorder.bind(decision_run_ids=["run-1"])

    row = json.loads(target.read_text(encoding="utf-8"))
    assert row["contract"] == CONTRACT
    assert row["trace_id"] == "trace-1"
    assert row["stage"] == "decision_log_read"
    assert row["elapsed_ms"] == 125.5
    assert row["started_at"] == START.isoformat()
    assert row["finished_at"] == (START + timedelta(seconds=10)).isoformat()
    assert row["metadata"]["decision_run_ids"] == ["run-1"]
    assert row["detail"] == {"rows": 12}


def test_records_failed_stage_without_swallowing_error(tmp_path) -> None:
    timestamps = iter((START, START + timedelta(milliseconds=1)))
    monotonic = iter((10, 20))
    recorder = StageTimingRecorder(
        tmp_path / "stages.jsonl",
        pipeline="virtual_portfolio",
        clock=lambda: next(timestamps),
        monotonic_ns=lambda: next(monotonic),
    )

    with pytest.raises(RuntimeError, match="boom"):
        with recorder.stage("snapshot_read"):
            raise RuntimeError("boom")

    assert recorder.records[0]["status"] == "error"
    assert recorder.records[0]["error_type"] == "RuntimeError"


def test_records_handled_failure_as_non_ok(tmp_path) -> None:
    timestamps = iter((START, START + timedelta(milliseconds=1)))
    monotonic = iter((10, 20))
    recorder = StageTimingRecorder(
        tmp_path / "stages.jsonl",
        pipeline="virtual_portfolio",
        clock=lambda: next(timestamps),
        monotonic_ns=lambda: next(monotonic),
    )

    with recorder.stage("replay") as outcome:
        outcome.mark_handled_error("ReplayFailedClosed")

    assert recorder.records[0]["status"] == "handled_error"
    assert recorder.records[0]["error_type"] == "ReplayFailedClosed"
    assert summarize(recorder.records)["failed_stages"] == ["replay"]


def test_busy_timing_log_drops_record_without_blocking_pipeline(tmp_path, capfd) -> None:
    target = tmp_path / "stages.jsonl"
    descriptor = os.open(target, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    timestamps = iter((START, START + timedelta(milliseconds=1)))
    monotonic = iter((10, 20))
    recorder = StageTimingRecorder(
        target,
        pipeline="briefing",
        clock=lambda: next(timestamps),
        monotonic_ns=lambda: next(monotonic),
    )

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            with recorder.stage("snapshot"):
                pass
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)

    assert recorder.write_error == "timing log is busy; stage record was dropped"
    assert target.read_text(encoding="utf-8") == ""
    assert "timing log is busy" in capfd.readouterr().err


def test_partial_append_is_completed_under_the_lock(tmp_path, monkeypatch) -> None:
    target = tmp_path / "stages.jsonl"
    timestamps = iter((START, START + timedelta(milliseconds=1)))
    monotonic = iter((10, 20))
    recorder = StageTimingRecorder(
        target,
        pipeline="briefing",
        clock=lambda: next(timestamps),
        monotonic_ns=lambda: next(monotonic),
    )
    real_write = os.write
    calls = 0

    def partial_write(descriptor, payload):
        nonlocal calls
        calls += 1
        if calls == 1:
            return real_write(descriptor, payload[:7])
        return real_write(descriptor, payload)

    monkeypatch.setattr(stage_timing.os, "write", partial_write)

    with recorder.stage("snapshot"):
        pass

    assert calls >= 2
    assert json.loads(target.read_text(encoding="utf-8"))["stage"] == "snapshot"


def test_disabled_recorder_keeps_in_memory_summary() -> None:
    timestamps = iter((START, START))
    monotonic = iter((1, 2))
    recorder = StageTimingRecorder(
        None,
        pipeline="briefing",
        clock=lambda: next(timestamps),
        monotonic_ns=lambda: next(monotonic),
    )

    with recorder.stage("build"):
        pass

    assert recorder.enabled is False
    assert summarize(recorder.records) == {
        "contract": CONTRACT,
        "stage_count": 1,
        "elapsed_ms_by_stage": {"build": 0.0},
        "failed_stages": [],
    }
