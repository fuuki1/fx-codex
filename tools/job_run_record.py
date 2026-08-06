"""Persist a durable execution record for one scheduled job run.

P1: two launchd jobs (`com.fx-codex.datafeed-collector`, exit 69, and
`com.fx-codex.operational-sync`, exit 2) have been failing on the Mac mini
while their stderr logs stayed empty or absent, so the failure reason existed
nowhere. `launchctl list` keeps only the most recent exit status and no
context at all.

This wrapper runs a command and appends one JSON line describing the run:
exit code, argv, working directory, code provenance, stdout/stderr tails, and
a coarse failure classification. It also maintains a small state file holding
the last success, so "how long has this been broken" is answerable without
reading the whole history.

The wrapper never changes the command's own exit code: it is observability
only, and a scheduler that treats non-zero as failure keeps behaving the same.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

TAIL_CHARS = 4_000

# Exit codes worth naming. 64-78 are the sysexits.h range that several tools in
# this repository return; everything else is reported as its raw number so a
# reader is never shown a guess.
_SYSEXITS = {
    64: "usage_error",
    65: "data_error",
    66: "no_input",
    67: "no_user",
    68: "no_host",
    69: "service_unavailable",
    70: "internal_software_error",
    71: "os_error",
    72: "os_file_missing",
    73: "cant_create_output",
    74: "io_error",
    75: "temporary_failure",
    76: "protocol_error",
    77: "permission_denied",
    78: "config_error",
}


def classify_exit(code: int) -> str:
    """Return a coarse, non-speculative label for one exit code."""

    if code == 0:
        return "success"
    if code < 0:
        return f"signal_{-code}"
    if code in _SYSEXITS:
        return _SYSEXITS[code]
    if code == 127:
        return "command_not_found"
    if code == 126:
        return "not_executable"
    if code > 128:
        return f"signal_{code - 128}"
    return f"exit_{code}"


def _as_text(value: object) -> str:
    """Normalise captured output, which may be bytes on timeout."""

    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value if isinstance(value, str) else ""


def _tail(text: str, limit: int = TAIL_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[-limit:]


def _code_provenance(cwd: Path) -> dict[str, object]:
    """Record which code actually ran, including uncommitted drift.

    The production tree carries tracked modifications, so a bare commit SHA
    would misidentify the code under test.
    """

    def _git(*args: str) -> str | None:
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode != 0:
            return None
        return result.stdout.strip()

    head = _git("rev-parse", "HEAD")
    branch = _git("rev-parse", "--abbrev-ref", "HEAD")
    status = _git("status", "--porcelain")
    return {
        "head": head,
        "branch": branch,
        "dirty_tracked_files": (
            None
            if status is None
            else len([line for line in status.splitlines() if not line.startswith("??")])
        ),
    }


def run(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run a command and persist a durable execution record.",
    )
    parser.add_argument("--name", required=True, help="job name, e.g. datafeed-collector")
    parser.add_argument("--record", required=True, help="JSONL path for run records")
    parser.add_argument("--state", help="optional JSON path holding last-success state")
    parser.add_argument("--timeout-seconds", type=float, default=None)
    parser.add_argument(
        "--step",
        help=(
            "optional step label, e.g. lag=4. Recorded so a multi-step job's "
            "individual failures survive later successful steps."
        ),
    )
    parser.add_argument(
        "--run-id",
        help="optional identifier grouping the steps of one job invocation",
    )
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(list(argv) if argv is not None else None)

    command = [part for part in args.command if part != "--"]
    if not command:
        parser.error("a command is required")

    cwd = Path.cwd()
    started = datetime.now(UTC)
    timed_out = False
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=args.timeout_seconds,
            check=False,
        )
        exit_code = completed.returncode
        stdout, stderr = completed.stdout, completed.stderr
    except subprocess.TimeoutExpired as expired:
        timed_out = True
        exit_code = 124
        stdout = _as_text(expired.stdout)
        stderr = _as_text(expired.stderr)
    except OSError as error:
        timed_out = False
        exit_code = 127
        stdout = ""
        stderr = f"failed to launch command: {error}"

    finished = datetime.now(UTC)
    record: dict[str, object] = {
        "contract": "job-run-record-v2",
        "job_name": args.name,
        "step": args.step,
        "run_id": args.run_id,
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "duration_seconds": (finished - started).total_seconds(),
        "command": command,
        "working_directory": str(cwd),
        "exit_code": exit_code,
        "failure_class": "timeout" if timed_out else classify_exit(exit_code),
        "succeeded": exit_code == 0,
        "stdout_tail": _tail(stdout),
        "stderr_tail": _tail(stderr),
        "stdout_bytes": len(stdout),
        "stderr_bytes": len(stderr),
        "code_provenance": _code_provenance(cwd),
        "environment": {
            "user": os.environ.get("USER"),
            "path_head": (os.environ.get("PATH") or "").split(":")[0],
        },
    }

    record_path = Path(args.record)
    record_path.parent.mkdir(parents=True, exist_ok=True)
    with record_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    if args.state:
        state_path = Path(args.state)
        state_path.parent.mkdir(parents=True, exist_ok=True)
        previous: dict[str, object] = {}
        if state_path.exists():
            try:
                loaded = json.loads(state_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    previous = loaded
            except (OSError, ValueError):
                previous = {}
        prior_failures = previous.get("consecutive_failures")
        prior_count = prior_failures if isinstance(prior_failures, int) else 0
        consecutive = 0 if exit_code == 0 else prior_count + 1

        # Per-step history must not be erased by a later successful step: the
        # datafeed collector's lag=4 failure was invisible precisely because
        # lag=1 overwrote the shared state file. Keyed by step, each entry
        # keeps its own last success and last failure.
        prior_steps = previous.get("steps")
        steps: dict[str, object] = dict(prior_steps) if isinstance(prior_steps, dict) else {}
        if args.step:
            prior_entry = steps.get(args.step)
            entry = dict(prior_entry) if isinstance(prior_entry, dict) else {}
            entry["last_run_at"] = finished.isoformat()
            entry["last_exit_code"] = exit_code
            entry["last_failure_class"] = record["failure_class"]
            entry["last_duration_seconds"] = record["duration_seconds"]
            if exit_code == 0:
                entry["last_success_at"] = finished.isoformat()
            else:
                entry["last_failure_at"] = finished.isoformat()
                entry["last_failure_stderr_tail"] = _tail(stderr, 500)
            steps[args.step] = entry

        state = {
            "contract": "job-run-state-v2",
            "job_name": args.name,
            "last_run_at": finished.isoformat(),
            "last_exit_code": exit_code,
            "last_failure_class": record["failure_class"],
            "consecutive_failures": consecutive,
            "last_success_at": (
                finished.isoformat() if exit_code == 0 else previous.get("last_success_at")
            ),
            "steps": steps,
        }
        state_path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    # Observability must not change scheduling behaviour.
    sys.stdout.write(stdout)
    sys.stderr.write(stderr)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(run())
