#!/usr/bin/env python3
"""Run the read-only Massive FX BBO collector under a single-writer lock."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import signal
import stat
import sys
import time
from types import FrameType
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from data_platform.collect.massive import (  # noqa: E402
    ENV_API_KEY,
    CollectorConfigError,
    MassiveConfig,
    stream_quotes,
    websocket_transport,
)
from data_platform.collect.raw_first import QuoteLog  # noqa: E402
from data_platform.collect.reconnect import ConnectionState  # noqa: E402
from data_platform.raw.immutable_store import ImmutableRawStore  # noqa: E402
from tools.fx_quote_collector import (  # noqa: E402
    EX_CONFIG,
    EX_IOERR,
    EX_NOPERM,
    EX_OK,
    EX_SOFTWARE,
    EX_TEMPFAIL,
    EX_UNAVAILABLE,
    _persist_terminal_state,
    _record_incident,
)
from tools.run_exclusive import ExclusiveLock  # noqa: E402

DEFAULT_INSTRUMENTS = ("USDJPY", "EURUSD", "GBPUSD")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--instruments", nargs="+", default=list(DEFAULT_INSTRUMENTS))
    parser.add_argument("--max-messages", type=int, default=None)
    parser.add_argument(
        "--env-file",
        type=Path,
        default=None,
        help="optional mode-600 file containing only FX_MASSIVE_API_KEY",
    )
    parser.add_argument("--dry-run", action="store_true")
    return parser


def _load_env_file(path: Path) -> dict[str, str]:
    try:
        metadata = path.stat()
    except OSError as error:
        raise CollectorConfigError(f"cannot read Massive collector env file: {path}") from error
    if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
        raise CollectorConfigError("Massive collector env file must be a mode-600 regular file")
    if metadata.st_uid != os.getuid():
        raise CollectorConfigError("Massive collector env file must be owned by the current user")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise CollectorConfigError(f"cannot read Massive collector env file: {path}") from error
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise CollectorConfigError(
                f"invalid Massive env line {line_number}: expected KEY=VALUE"
            )
        key, raw_value = line.split("=", 1)
        key = key.strip()
        value = raw_value.strip()
        if key != ENV_API_KEY:
            raise CollectorConfigError(f"unsupported key in Massive collector env: {key}")
        if key in values:
            raise CollectorConfigError(f"duplicate key in Massive collector env: {key}")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        values[key] = value
    return values


def _counts(results: list[Any]) -> tuple[int, int]:
    accepted = sum(result.accepted_count for result in results)
    quarantined = sum(len(result.quarantined) for result in results)
    return accepted, quarantined


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        environment = _load_env_file(args.env_file) if args.env_file is not None else None
        config = MassiveConfig.from_env(environment)
        topics = config.subscription_topics(args.instruments)
    except CollectorConfigError as error:
        print(f"[massive-collector] config error: {error}", file=sys.stderr)
        print(f"[massive-collector] required env var: {ENV_API_KEY}", file=sys.stderr)
        return EX_CONFIG

    plan = {
        "config": repr(config),
        "instruments": list(args.instruments),
        "topics": list(topics),
        "output_root": str(args.output_root),
        "endpoint_class": "Massive Forex BBO WebSocket (read-only market data)",
        "broker_connected": False,
    }
    if args.dry_run:
        print(json.dumps({"dry_run": True, **plan}, indent=2))
        return EX_OK

    started = datetime.now(UTC)
    lock = ExclusiveLock("quote-collector", locks_dir=args.output_root / "state")
    if not lock.acquire():
        detail = "another provider already holds the shared quote-collector lock"
        try:
            _record_incident(
                args.output_root,
                incident_type="duplicate_writer_rejected",
                severity="critical",
                detail=detail,
                started=started,
            )
            _persist_terminal_state(
                args.output_root,
                started=started,
                status="duplicate_writer_rejected",
                exit_code=EX_TEMPFAIL,
                state=None,
                accepted=0,
                quarantined=0,
                graceful_stop_requested=False,
                error_type="ExclusiveLockCollision",
                error_detail=detail,
            )
        except OSError as error:
            print(f"[massive-collector] failed to persist lock incident: {error}", file=sys.stderr)
        return EX_TEMPFAIL

    state: ConnectionState | None = None
    results: list[Any] = []
    stop_requested = {"flag": False}
    try:
        store = ImmutableRawStore(args.output_root / "raw")
        log = QuoteLog(args.output_root / "log")

        def _graceful(_signum: int, _frame: FrameType | None) -> None:
            stop_requested["flag"] = True

        def _sleep_interruptibly(seconds: float) -> None:
            deadline = time.monotonic() + max(0.0, seconds)
            while not stop_requested["flag"]:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                time.sleep(min(0.25, remaining))

        signal.signal(signal.SIGTERM, _graceful)
        signal.signal(signal.SIGINT, _graceful)
        state, results = stream_quotes(
            config,
            args.instruments,
            store=store,
            log=log,
            transport=websocket_transport,
            max_messages=args.max_messages,
            sleeper=_sleep_interruptibly,
            should_stop=lambda: stop_requested["flag"],
            source_endpoint_class="forex_bbo_websocket",
            collection_mode="live_stream",
        )
        accepted, quarantined = _counts(results)
        stopped_reason = state.stopped_reason or "stream_returned"
        if "token_expired" in stopped_reason:
            exit_code, status = EX_NOPERM, "authorization_failed"
            _record_incident(
                args.output_root,
                incident_type="massive_collector_authorization_failure",
                severity="critical",
                detail=stopped_reason,
                started=started,
            )
        elif stopped_reason == "max_reconnects_exhausted":
            exit_code, status = EX_UNAVAILABLE, "source_unavailable"
            _record_incident(
                args.output_root,
                incident_type="massive_collector_source_unavailable",
                severity="critical",
                detail=stopped_reason,
                started=started,
            )
        else:
            exit_code, status = EX_OK, "stopped"
        _persist_terminal_state(
            args.output_root,
            started=started,
            status=status,
            exit_code=exit_code,
            state=state,
            accepted=accepted,
            quarantined=quarantined,
            graceful_stop_requested=stop_requested["flag"],
        )
        return exit_code
    except OSError as error:
        accepted, quarantined = _counts(results)
        try:
            _record_incident(
                args.output_root,
                incident_type="massive_collector_io_failure",
                severity="critical",
                detail=f"{type(error).__name__}: {error}",
                started=started,
            )
            _persist_terminal_state(
                args.output_root,
                started=started,
                status="io_failure",
                exit_code=EX_IOERR,
                state=state,
                accepted=accepted,
                quarantined=quarantined,
                graceful_stop_requested=stop_requested["flag"],
                error_type=type(error).__name__,
                error_detail=str(error),
            )
        except OSError:
            pass
        return EX_IOERR
    except Exception as error:
        accepted, quarantined = _counts(results)
        try:
            _record_incident(
                args.output_root,
                incident_type="massive_collector_runtime_failure",
                severity="critical",
                detail=f"{type(error).__name__}: {error}",
                started=started,
            )
            _persist_terminal_state(
                args.output_root,
                started=started,
                status="runtime_failure",
                exit_code=EX_SOFTWARE,
                state=state,
                accepted=accepted,
                quarantined=quarantined,
                graceful_stop_requested=stop_requested["flag"],
                error_type=type(error).__name__,
                error_detail=str(error),
            )
        except OSError:
            pass
        return EX_SOFTWARE
    finally:
        lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
