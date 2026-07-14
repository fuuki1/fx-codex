#!/usr/bin/env python3
"""Read-only FX quote collector daemon (launchd entrypoint).

Runs a live pricing source through the raw-first ingest pipeline under a
single-writer exclusive lock. This process is structurally incapable of
trading: it imports only ``data_platform.collect`` (no executor, no order
endpoint — enforced by tests).

Sources (``--source``):
- ``oanda``  (default) broker pricing stream; requires read-only credentials
- ``truefx`` unauthenticated LIVE aggregator snapshot poller; requires no
  credentials at all. TrueFX is NOT a broker — its quotes are indicative and
  always ``tradable=False``; the scorecard credits it only as aggregator-live.

Fail-closed rules:
- missing credentials (oanda) -> exit 78 (EX_CONFIG) without touching the log
- lock already held (another writer) -> exit 75 (EX_TEMPFAIL), incident logged
- token expiry mid-stream (oanda) -> stop (no retry), exit 77 (EX_NOPERM)

``--dry-run`` validates configuration presence (NOT the token's validity),
prints the collection plan with any token masked, and exits 0/78. launchd
install scripts call this before loading the job.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
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
from data_platform.collect import truefx  # noqa: E402
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
        "--source",
        choices=("oanda", "truefx"),
        default="oanda",
        help="live pricing source (truefx needs no credentials; aggregator, tradable=False)",
    )
    parser.add_argument(
        "--poll-interval-seconds",
        type=float,
        default=truefx.DEFAULT_POLL_INTERVAL_SECONDS,
        help="snapshot poll cadence for --source truefx",
    )
    parser.add_argument(
        "--max-duration-minutes",
        type=float,
        default=None,
        help="stop after this long (truefx); default runs until signalled",
    )
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


def _acquire_lock_or_incident(output_root: Path) -> ExclusiveLock | None:
    """Single-writer lock; a refused acquisition persists a critical incident."""

    lock = ExclusiveLock("quote-collector", locks_dir=output_root / "state")
    if lock.acquire():
        return lock
    incident = {
        "occurred_at": datetime.now(UTC).isoformat(),
        "type": "duplicate_writer_rejected",
        "severity": "critical",
        "writer_pid": os.getpid(),
        "output_root": str(output_root),
    }
    try:
        _write_state(_lock_collision_incident_path(output_root), incident)
    except OSError as error:
        print(f"[collector] failed to persist lock incident: {error}", file=sys.stderr)
    print("[collector] another writer holds the lock; refusing to double-write")
    return None


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.source == "truefx":
        return _run_truefx(args)
    return _run_oanda(args)


def _run_truefx(args: argparse.Namespace) -> int:
    plan = {
        "source": "truefx",
        "instruments": list(args.instruments),
        "output_root": str(args.output_root),
        "endpoint_class": "streaming_pricing (read-only, unauthenticated aggregator)",
        "poll_interval_seconds": args.poll_interval_seconds,
        "credentials_required": False,
        "tradable": False,
    }
    if args.dry_run:
        print(json.dumps({"dry_run": True, **plan}, indent=2))
        return EX_OK
    lock = _acquire_lock_or_incident(args.output_root)
    if lock is None:
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
        max_duration = (
            None
            if args.max_duration_minutes is None
            else timedelta(minutes=args.max_duration_minutes)
        )
        state, results = truefx.run_poller(
            fetcher=truefx.requests_fetcher,
            store=store,
            log=log,
            instruments=list(args.instruments),
            poll_interval_seconds=args.poll_interval_seconds,
            max_polls=args.max_messages,
            max_duration=max_duration,
            should_stop=lambda: stop_requested["flag"],
            sleeper=_sleep_interruptibly,
        )
        accepted = sum(result.accepted_count for result in results)
        quarantined = sum(len(result.quarantined) for result in results)
        _write_state(
            args.output_root / "state" / "last_run.json",
            {
                "source": "truefx",
                "started_at": started.isoformat(),
                "finished_at": datetime.now(UTC).isoformat(),
                "polls": len(results),
                "accepted_quotes": accepted,
                "quarantined": quarantined,
                "connection": state.to_dict(),
                "graceful_stop_requested": stop_requested["flag"],
            },
        )
        return EX_OK
    finally:
        lock.release()


def _run_oanda(args: argparse.Namespace) -> int:
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

    lock = _acquire_lock_or_incident(args.output_root)
    if lock is None:
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
