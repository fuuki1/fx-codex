#!/usr/bin/env python3
"""Read-only FX quote collector daemon (launchd entrypoint).

Runs the OANDA pricing stream through the raw-first ingest pipeline under a
single-writer exclusive lock. This process is structurally incapable of
trading: it imports only ``data_platform.collect`` (no executor, no order
endpoint — enforced by tests) and its credentials are read-only pricing
scope.

Fail-closed rules:
- missing credentials -> exit 78 (EX_CONFIG) without touching the quote log
- lock already held -> exit 75 (EX_TEMPFAIL), incident and terminal state logged
- token expiry mid-stream -> stop (no retry), exit 77 (EX_NOPERM)
- I/O failure -> exit 74 (EX_IOERR), incident and terminal state attempted
- unexpected runtime failure -> exit 70 (EX_SOFTWARE), incident recorded

``--dry-run`` validates configuration presence (not token validity), prints the
collection plan with all credential values masked, and exits 0/78.
"""

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
from data_platform.collect.reconnect import ConnectionState  # noqa: E402
from data_platform.raw.immutable_store import ImmutableRawStore  # noqa: E402
from tools.run_exclusive import ExclusiveLock  # noqa: E402

EX_OK = 0
EX_SOFTWARE = 70
EX_TEMPFAIL = 75
EX_IOERR = 74
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
        "--env-file",
        type=Path,
        default=None,
        help="optional mode-600 KEY=VALUE file containing only FX_OANDA_* credentials",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate config and print the plan without connecting",
    )
    return parser


def _load_env_file(path: Path) -> dict[str, str]:
    """Parse a narrow dotenv file without evaluating shell code."""

    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError as error:
        raise CollectorConfigError(f"cannot read collector env file: {path}") from error
    if mode != 0o600:
        raise CollectorConfigError(f"collector env file must be mode 600, got {mode:o}: {path}")

    allowed = {ENV_TOKEN, ENV_ACCOUNT, ENV_ENVIRONMENT}
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise CollectorConfigError(f"cannot read collector env file: {path}") from error
    for line_number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise CollectorConfigError(
                f"invalid collector env line {line_number}: expected KEY=VALUE"
            )
        key, raw_value = line.split("=", 1)
        key = key.strip()
        value = raw_value.strip()
        if key not in allowed:
            raise CollectorConfigError(
                f"unsupported key in collector env file at line {line_number}: {key}"
            )
        if key in values:
            raise CollectorConfigError(f"duplicate key in collector env file: {key}")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        values[key] = value
    return values


def _fsync_directory(directory: Path) -> None:
    try:
        descriptor = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _write_state(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _incident_path(output_root: Path, incident_type: str) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    safe_type = "".join(character if character.isalnum() else "_" for character in incident_type)
    return output_root / "state" / "incidents" / f"{safe_type}_{stamp}_{os.getpid()}.json"


def _connection_payload(state: ConnectionState | None) -> dict[str, Any] | None:
    return None if state is None else state.to_dict()


def _persist_terminal_state(
    output_root: Path,
    *,
    started: datetime,
    status: str,
    exit_code: int,
    state: ConnectionState | None,
    accepted: int,
    quarantined: int,
    graceful_stop_requested: bool,
    error_type: str | None = None,
    error_detail: str | None = None,
) -> None:
    payload: dict[str, object] = {
        "started_at": started.isoformat(),
        "finished_at": datetime.now(UTC).isoformat(),
        "status": status,
        "exit_code": exit_code,
        "accepted_quotes": accepted,
        "quarantined": quarantined,
        "connection": _connection_payload(state),
        "graceful_stop_requested": graceful_stop_requested,
    }
    if error_type is not None:
        payload["error_type"] = error_type
    if error_detail is not None:
        payload["error_detail"] = error_detail[:500]
    _write_state(output_root / "state" / "last_run.json", payload)


def _record_incident(
    output_root: Path,
    *,
    incident_type: str,
    severity: str,
    detail: str,
    started: datetime,
) -> None:
    _write_state(
        _incident_path(output_root, incident_type),
        {
            "occurred_at": datetime.now(UTC).isoformat(),
            "started_at": started.isoformat(),
            "type": incident_type,
            "severity": severity,
            "writer_pid": os.getpid(),
            "output_root": str(output_root),
            "detail": detail[:500],
        },
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        environment = _load_env_file(args.env_file) if args.env_file is not None else None
        config = OandaConfig.from_env(environment)
    except CollectorConfigError as error:
        print(f"[collector] config error: {error}", file=sys.stderr)
        print(
            f"[collector] required env vars: {ENV_TOKEN}, {ENV_ACCOUNT}, {ENV_ENVIRONMENT}",
            file=sys.stderr,
        )
        return EX_CONFIG

    plan = {
        "config": repr(config),
        "instruments": list(args.instruments),
        "output_root": str(args.output_root),
        "endpoint_class": "streaming_pricing (read-only)",
    }
    if args.dry_run:
        print(json.dumps({"dry_run": True, **plan}, indent=2))
        return EX_OK

    started = datetime.now(UTC)
    lock = ExclusiveLock("quote-collector", locks_dir=args.output_root / "state")
    if not lock.acquire():
        detail = "another writer holds the quote-collector lock"
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
            print(f"[collector] failed to persist lock incident: {error}", file=sys.stderr)
        print("[collector] another writer holds the lock; refusing to double-write")
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
                if remaining <= 0.0:
                    return
                time.sleep(min(0.25, remaining))

        signal.signal(signal.SIGTERM, _graceful)
        signal.signal(signal.SIGINT, _graceful)
        state, results = stream_quotes(
            config,
            args.instruments,
            store=store,
            log=log,
            transport=requests_transport,
            max_messages=args.max_messages,
            sleeper=_sleep_interruptibly,
            should_stop=lambda: stop_requested["flag"],
            source_endpoint_class="streaming_pricing",
            collection_mode="live_stream",
        )
        accepted = sum(result.accepted_count for result in results)
        quarantined = sum(len(result.quarantined) for result in results)
        exit_code = (
            EX_NOPERM if state.stopped_reason and "token_expired" in state.stopped_reason else EX_OK
        )
        _persist_terminal_state(
            args.output_root,
            started=started,
            status="stopped" if exit_code == EX_OK else "authorization_failed",
            exit_code=exit_code,
            state=state,
            accepted=accepted,
            quarantined=quarantined,
            graceful_stop_requested=stop_requested["flag"],
        )
        return exit_code
    except OSError as error:
        accepted = sum(result.accepted_count for result in results)
        quarantined = sum(len(result.quarantined) for result in results)
        detail = f"{type(error).__name__}: {error}"
        try:
            _record_incident(
                args.output_root,
                incident_type="collector_io_failure",
                severity="critical",
                detail=detail,
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
        except OSError as persistence_error:
            print(
                f"[collector] failed to persist I/O incident: {persistence_error}",
                file=sys.stderr,
            )
        print(f"[collector] I/O failure: {type(error).__name__}", file=sys.stderr)
        return EX_IOERR
    except Exception as error:
        accepted = sum(result.accepted_count for result in results)
        quarantined = sum(len(result.quarantined) for result in results)
        detail = f"{type(error).__name__}: {error}"
        try:
            _record_incident(
                args.output_root,
                incident_type="collector_runtime_failure",
                severity="critical",
                detail=detail,
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
        except OSError as persistence_error:
            print(
                f"[collector] failed to persist runtime incident: {persistence_error}",
                file=sys.stderr,
            )
        print(f"[collector] runtime failure: {type(error).__name__}", file=sys.stderr)
        return EX_SOFTWARE
    finally:
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
