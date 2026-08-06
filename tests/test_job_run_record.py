"""P1: durable execution records for scheduled jobs.

The Mac mini has been running `com.fx-codex.datafeed-collector` at exit 69 and
`com.fx-codex.operational-sync` at exit 2 with empty or absent stderr logs, so
no failure reason was recorded anywhere. These tests pin the contract that
makes such a failure explainable after the fact.
"""

from __future__ import annotations

import json
from pathlib import Path

from tools.job_run_record import classify_exit, run


def _records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_records_a_successful_run(tmp_path: Path) -> None:
    record = tmp_path / "runs.jsonl"
    exit_code = run(
        ["--name", "demo", "--record", str(record), "--", "/bin/echo", "hello"],
    )

    assert exit_code == 0
    entries = _records(record)
    assert len(entries) == 1
    assert entries[0]["succeeded"] is True
    assert entries[0]["failure_class"] == "success"
    assert entries[0]["command"] == ["/bin/echo", "hello"]
    assert "hello" in entries[0]["stdout_tail"]
    assert entries[0]["working_directory"]


def test_records_the_exit_69_case_that_was_invisible(tmp_path: Path) -> None:
    """The datafeed collector's real failure mode: non-zero exit, no stderr."""

    record = tmp_path / "runs.jsonl"
    exit_code = run(
        ["--name", "datafeed-collector", "--record", str(record), "--", "/bin/sh", "-c", "exit 69"],
    )

    assert exit_code == 69
    entry = _records(record)[0]
    assert entry["succeeded"] is False
    assert entry["exit_code"] == 69
    assert entry["failure_class"] == "service_unavailable"
    # The reason must be recorded even though the command printed nothing.
    assert entry["stderr_tail"] == ""
    assert entry["job_name"] == "datafeed-collector"


def test_state_tracks_last_success_and_consecutive_failures(tmp_path: Path) -> None:
    record = tmp_path / "runs.jsonl"
    state = tmp_path / "state.json"
    common = ["--name", "demo", "--record", str(record), "--state", str(state), "--"]

    run([*common, "/bin/sh", "-c", "exit 0"])
    first_success = json.loads(state.read_text(encoding="utf-8"))["last_success_at"]
    assert first_success is not None

    run([*common, "/bin/sh", "-c", "exit 2"])
    run([*common, "/bin/sh", "-c", "exit 2"])
    payload = json.loads(state.read_text(encoding="utf-8"))

    assert payload["consecutive_failures"] == 2
    assert payload["last_exit_code"] == 2
    # A stale success timestamp is what makes "broken since when" answerable.
    assert payload["last_success_at"] == first_success

    run([*common, "/bin/sh", "-c", "exit 0"])
    recovered = json.loads(state.read_text(encoding="utf-8"))
    assert recovered["consecutive_failures"] == 0
    assert recovered["last_success_at"] != first_success


def test_records_a_missing_command_without_raising(tmp_path: Path) -> None:
    record = tmp_path / "runs.jsonl"
    exit_code = run(
        ["--name", "demo", "--record", str(record), "--", "/nonexistent/binary"],
    )

    assert exit_code == 127
    entry = _records(record)[0]
    assert entry["succeeded"] is False
    assert "failed to launch command" in entry["stderr_tail"]


def test_records_capture_code_provenance(tmp_path: Path) -> None:
    """Production runs carry uncommitted drift, so a bare SHA would mislead."""

    record = tmp_path / "runs.jsonl"
    run(["--name", "demo", "--record", str(record), "--", "/bin/echo", "x"])
    provenance = _records(record)[0]["code_provenance"]

    assert set(provenance) == {"head", "branch", "dirty_tracked_files"}


def test_classify_exit_labels_known_and_unknown_codes() -> None:
    assert classify_exit(0) == "success"
    assert classify_exit(69) == "service_unavailable"
    assert classify_exit(2) == "exit_2"
    assert classify_exit(127) == "command_not_found"
    assert classify_exit(137) == "signal_9"


def test_per_step_failures_survive_later_successful_steps(tmp_path: Path) -> None:
    """The exact datafeed-collector defect: lag=1 must not erase lag=4's failure.

    The production collector overwrote a shared state file on every lag, so the
    only failing lag (4) was invisible while the state file reported exit 0.
    """

    record = tmp_path / "runs.jsonl"
    state = tmp_path / "state.json"

    def _step(step: str, code: int) -> int:
        return run(
            [
                "--name",
                "datafeed-collector",
                "--record",
                str(record),
                "--state",
                str(state),
                "--run-id",
                "run-1",
                "--step",
                step,
                "--",
                "/bin/sh",
                "-c",
                f"exit {code}",
            ]
        )

    assert _step("lag=4", 69) == 69
    assert _step("lag=3", 0) == 0
    assert _step("lag=2", 0) == 0
    assert _step("lag=1", 0) == 0

    payload = json.loads(state.read_text(encoding="utf-8"))
    steps = payload["steps"]

    # The failing step retains its own verdict.
    assert steps["lag=4"]["last_exit_code"] == 69
    assert steps["lag=4"]["last_failure_class"] == "service_unavailable"
    assert "last_failure_at" in steps["lag=4"]
    assert "last_success_at" not in steps["lag=4"]

    # The succeeding steps are recorded separately.
    assert steps["lag=1"]["last_exit_code"] == 0
    assert "last_success_at" in steps["lag=1"]

    # Every step also has its own durable record line.
    entries = _records(record)
    assert [e["step"] for e in entries] == ["lag=4", "lag=3", "lag=2", "lag=1"]
    assert all(e["run_id"] == "run-1" for e in entries)
    failed = [e for e in entries if not e["succeeded"]]
    assert len(failed) == 1
    assert failed[0]["step"] == "lag=4"
    assert failed[0]["duration_seconds"] >= 0


def test_step_records_carry_timing_for_each_step(tmp_path: Path) -> None:
    record = tmp_path / "runs.jsonl"
    run(
        [
            "--name",
            "j",
            "--record",
            str(record),
            "--step",
            "lag=4",
            "--",
            "/bin/sh",
            "-c",
            "exit 0",
        ]
    )
    entry = _records(record)[0]

    assert entry["step"] == "lag=4"
    assert entry["started_at"] < entry["finished_at"]
    assert entry["contract"] == "job-run-record-v2"
