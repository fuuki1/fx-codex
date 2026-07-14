#!/usr/bin/env python3
"""Read-only FX quote collector daemon (launchd entrypoint).

Runs the OANDA pricing stream through the raw-first ingest pipeline under a
single-writer exclusive lock. This process is structurally incapable of
trading: it imports only ``data_platform.collect`` (no executor, no order
endpoint — enforced by tests) and its credentials are read-only pricing
scope.

Fail-closed rules:
- missing credentials -> exit 78 (EX_CONFIG) without touching the log
- lock already held (another writer) -> exit 75 (EX_TEMPFAIL), incident logged
- token expiry mid-stream -> stop (no retry), exit 77 (EX_NOPERM)

``--dry-run`` validates configuration presence (NOT the token's validity),
prints the collection plan with the token masked, and exits 0/78. launchd
install scripts call this before loading the job.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import signal
import sys
import time
from types import FrameType

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from data_platform.collect.oanda import (  # noqa: E402
    ENV_ACCOUNT,
    ENV_ENVIRONMENT,
    ENV_TOKEN,
    CollectorConfigError,
    OandaConfig,
    requests_transport,
    stream_quotes,
)
from data_platform.collect.raw_first import QuoteLog  # noqa: E402
from data_platform.raw.immutable_store import ImmutableRawStore  # noqa: E402
from tools.run_exclusive import ExclusiveLock  # noqa: E402

EX_OK = 0
EX_TEMPFAIL = 75
EX_NOPERM = 77
EX_CONFIG = 78

DEFAULT_INSTRUMENTS = ("USD_JPY", "EUR_USD", "GBP_USD")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-root",
        type=Path,
        required=True,
        help="collection root (raw/, log/, state/ live under it)",
    )
    parser.add_argument(
        "--instruments",
        nargs="+",
        default=list(DEFAULT_INSTRUMENTS),
        help="OANDA instrument names, e.g. USD_JPY",
    )
    parser.add_argument("--max-messages", type=int, default=None)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate config and print the plan without connecting",
    )
    return parser


def _write_state(path: Path, payload: dict[str, object]) -> None:
    """Durably replace one JSON state file without exposing partial content."""

    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _lock_collision_incident_path(output_root: Path) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    return output_root / "state" / "incidents" / f"lock_collision_{stamp}_{os.getpid()}.json"


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = OandaConfig.from_env()
    except CollectorConfigError as error:
        print(f"[collector] config error: {error}", file=sys.stderr)
        print(
            f"[collector] required env vars: {ENV_TOKEN}, {ENV_ACCOUNT}, {ENV_ENVIRONMENT}",
            file=sys.stderr,
        )
        return EX_CONFIG
    plan = {
        "config": repr(config),  # token masked by OandaConfig.__repr__
        "instruments": list(args.instruments),
        "output_root": str(args.output_root),
        "endpoint_class": "streaming_pricing (read-only)",
    }
    if args.dry_run:
        print(json.dumps({"dry_run": True, **plan}, indent=2))
        return EX_OK

    lock = ExclusiveLock("quote-collector", locks_dir=args.output_root / "state")
    if not lock.acquire():
        incident = {
            "occurred_at": datetime.now(UTC).isoformat(),
            "type": "duplicate_writer_rejected",
            "severity": "critical",
            "writer_pid": os.getpid(),
            "output_root": str(args.output_root),
        }
        try:
            _write_state(_lock_collision_incident_path(args.output_root), incident)
        except OSError as error:
            print(f"[collector] failed to persist lock incident: {error}", file=sys.stderr)
        print("[collector] another writer holds the lock; refusing to double-write")
        return EX_TEMPFAIL
    try:
        store = ImmutableRawStore(args.output_root / "raw")
        log = QuoteLog(args.output_root / "log")
        stop_requested = {"flag": False}

        def _graceful(_signum: int, _frame: FrameType | None) -> None:
            stop_requested["flag"] = True

        def _sleep_interruptibly(seconds: float) -> None:
            deadline = time.monotonic() + max(0.0, seconds)
            while not stop_requested["flag"]:
                remaining = deadline - time.monotonic()
                if remaining <= 0.0:
                    return
                time.sleep(min(0.25, remaining))

        signal.signal(signal.SIGTERM, _graceful)
        signal.signal(signal.SIGINT, _graceful)
        started = datetime.now(UTC)
        state, results = stream_quotes(
            config,
            args.instruments,
            store=store,
            log=log,
            transport=requests_transport,
            max_messages=args.max_messages,
            sleeper=_sleep_interruptibly,
            should_stop=lambda: stop_requested["flag"],
        )
        accepted = sum(result.accepted_count for result in results)
        quarantined = sum(len(result.quarantined) for result in results)
        _write_state(
            args.output_root / "state" / "last_run.json",
            {
                "started_at": started.isoformat(),
                "finished_at": datetime.now(UTC).isoformat(),
                "accepted_quotes": accepted,
                "quarantined": quarantined,
                "connection": state.to_dict(),
                "graceful_stop_requested": stop_requested["flag"],
            },
        )
        if state.stopped_reason and "token_expired" in state.stopped_reason:
            return EX_NOPERM
        return EX_OK
    finally:
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
