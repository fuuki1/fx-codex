#!/usr/bin/env python3
"""Run one existing analysis producer and publish a bounded lifecycle sidecar."""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from datetime import UTC, datetime
import os
from pathlib import Path
import signal
import subprocess
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fx_intel.runtime_status import (  # noqa: E402
    JOB_CONTRACT,
    collect_input_watermarks,
    publish_job_status,
    read_small_json,
)


def _previous_completed(path: Path, *, job_id: str) -> dict[str, object] | None:
    try:
        payload = read_small_json(path)
    except (OSError, ValueError):
        return None
    if payload.get("contract") != JOB_CONTRACT or payload.get("job_id") != job_id:
        return None
    if payload.get("state") == "completed":
        return {
            key: payload.get(key)
            for key in (
                "input_watermark",
                "build_started_at",
                "build_completed_at",
                "published_at",
            )
        }
    previous = payload.get("previous_completed")
    return dict(previous) if isinstance(previous, Mapping) else None


def _safe_publish(path: Path, **values: object) -> None:
    try:
        publish_job_status(path, **values)  # type: ignore[arg-type]
    except (OSError, TypeError, ValueError) as error:
        print(f"runtime status publish failed: {type(error).__name__}", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--log-dir", type=Path, required=True)
    parser.add_argument("--status-output", type=Path, required=True)
    parser.add_argument("--busy-exit-code", type=int, default=75)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = list(args.command)
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        parser.error("command is required after --")
    log_dir = args.log_dir.expanduser().resolve()
    status_output = args.status_output.expanduser().resolve()
    started = datetime.now(UTC)
    inputs = collect_input_watermarks(log_dir)
    previous = _previous_completed(status_output, job_id=args.job_id)
    _safe_publish(
        status_output,
        job_id=args.job_id,
        state="running",
        input_watermark=inputs,
        build_started_at=started.isoformat(),
        stage="command",
        progress={"phase": "running"},
        observed_at=started,
        previous_completed=previous,
    )
    child: subprocess.Popen[bytes] | None = None

    def forward(signum: int, _frame: object) -> None:
        if child is not None:
            try:
                os.killpg(child.pid, signum)
            except ProcessLookupError:
                pass

    previous_handlers = {
        signum: signal.signal(signum, forward) for signum in (signal.SIGTERM, signal.SIGINT)
    }
    try:
        try:
            child = subprocess.Popen(command, start_new_session=True)
            return_code = child.wait()
            failure_reason = None
        except OSError as error:
            return_code = 127
            failure_reason = f"launch_failed:{type(error).__name__}"
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
    completed = datetime.now(UTC)
    common = {
        "job_id": args.job_id,
        "input_watermark": inputs,
        "build_started_at": started.isoformat(),
        "build_completed_at": completed.isoformat(),
        "observed_at": completed,
        "previous_completed": previous,
    }
    if return_code == 0:
        _safe_publish(
            status_output,
            **common,
            state="completed",
            published_at=completed.isoformat(),
            stage="published",
            progress={"phase": "complete"},
        )
    elif return_code == args.busy_exit_code:
        _safe_publish(
            status_output,
            **common,
            state="blocked",
            stage="blocked",
            blocked_reason="shared_workload_slot_busy",
            progress={"phase": "not_started"},
        )
    else:
        _safe_publish(
            status_output,
            **common,
            state="failed",
            stage="failed",
            failure_reason=failure_reason or f"process_exit:{return_code}",
            progress={"phase": "failed"},
        )
    return 128 + abs(return_code) if return_code < 0 else return_code


if __name__ == "__main__":
    raise SystemExit(main())
