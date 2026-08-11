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
- source unavailable after reconnect budget -> exit 69 (EX_UNAVAILABLE)
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
import sys
import time
from types import FrameType
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
VENV_ROOT = REPO_ROOT / ".venv"
VENV_SITE_PACKAGES = (
    VENV_ROOT / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"
)
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(VENV_SITE_PACKAGES) not in sys.path:
    sys.path.append(str(VENV_SITE_PACKAGES))

from data_platform.collect.collector_env import (  # noqa: E402
    CollectorEnvironmentError,
    load_private_collector_env,
)
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
from data_platform.raw.event_ledger import RawEventLedger  # noqa: E402
from data_platform.raw.runtime_identity import (  # noqa: E402
    COLLECTOR_ENVIRONMENT_LOCK_PATH,
    RuntimeIdentityError,
    critical_collector_code_sha256,
    file_sha256,
    python_runtime_isolated,
    verified_collector_environment_identity,
)
from data_platform.raw.shadow import (  # noqa: E402
    RAW_LEDGER_FLAG,
    RawLedgerShadowWriter,
    raw_ledger_enabled,
)
from tools.run_exclusive import ExclusiveLock  # noqa: E402

EX_OK = 0
EX_UNAVAILABLE = 69
EX_SOFTWARE = 70
EX_IOERR = 74
EX_TEMPFAIL = 75
EX_NOPERM = 77
EX_CONFIG = 78

DEFAULT_INSTRUMENTS = ("USD_JPY", "EUR_USD", "GBP_USD")
COLLECTOR_ENV_KEYS = frozenset({ENV_TOKEN, ENV_ACCOUNT, ENV_ENVIRONMENT, RAW_LEDGER_FLAG})


def isolated_requests_transport(url: str, token: str):
    """Named production transport whose dependency closure is verified at startup."""

    return requests_transport(url, token)


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
        return load_private_collector_env(path, allowed_keys=COLLECTOR_ENV_KEYS)
    except CollectorEnvironmentError as error:
        raise CollectorConfigError(str(error)) from error


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


def _persist_shadow_metrics(
    output_root: Path,
    writer: RawLedgerShadowWriter,
    *,
    drained: bool,
    runtime_identity: dict[str, str],
) -> None:
    _write_state(
        output_root / "state" / "raw_ledger_shadow_metrics.json",
        {
            **writer.metrics(),
            **runtime_identity,
            "contract": "raw-ledger-shadow-metrics-v1",
            "final": True,
            "drained": drained,
            "writer_pid": os.getpid(),
            "observed_at": datetime.now(UTC).isoformat(),
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

    raw_shadow_enabled = raw_ledger_enabled(environment)
    runtime_identity: dict[str, str] = {}
    if raw_shadow_enabled:
        if not python_runtime_isolated():
            print("[collector] raw ledger requires isolated Python runtime", file=sys.stderr)
            return EX_CONFIG
        try:
            environment_sha, environment_lock_sha = verified_collector_environment_identity(
                REPO_ROOT / COLLECTOR_ENVIRONMENT_LOCK_PATH,
                environment_root=VENV_ROOT,
            )
            runtime_identity = {
                "critical_code_sha256": critical_collector_code_sha256(REPO_ROOT),
                "interpreter_sha256": file_sha256(Path(sys.executable).resolve()),
                "collector_environment_sha256": environment_sha,
                "collector_environment_lock_sha256": environment_lock_sha,
            }
        except (OSError, RuntimeIdentityError) as error:
            print(f"[collector] runtime identity rejected: {error}", file=sys.stderr)
            return EX_CONFIG
        args.output_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        args.output_root.chmod(0o700)

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
    raw_shadow_writer: RawLedgerShadowWriter | None = None
    stop_requested = {"flag": False}
    try:
        store = ImmutableRawStore(args.output_root / "raw")
        log = QuoteLog(args.output_root / "log")
        if raw_shadow_enabled:
            raw_shadow_writer = RawLedgerShadowWriter(
                RawEventLedger(args.output_root / "raw-ledger", blob_store=store),
                enabled=True,
                failure_log=args.output_root / "state" / "raw_ledger_shadow_failures.jsonl",
            )

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
            transport=isolated_requests_transport,
            max_messages=args.max_messages,
            sleeper=_sleep_interruptibly,
            should_stop=lambda: stop_requested["flag"],
            source_endpoint_class="streaming_pricing",
            collection_mode="live_stream",
            raw_shadow_writer=raw_shadow_writer,
        )
        if raw_shadow_writer is not None:
            drained = raw_shadow_writer.drain()
            _persist_shadow_metrics(
                args.output_root,
                raw_shadow_writer,
                drained=drained,
                runtime_identity=runtime_identity,
            )
            if not drained:
                raise RuntimeError("raw ledger shadow queue did not drain")
        accepted = sum(result.accepted_count for result in results)
        quarantined = sum(len(result.quarantined) for result in results)
        stopped_reason = state.stopped_reason or "stream_returned"
        if "token_expired" in stopped_reason:
            exit_code = EX_NOPERM
            status = "authorization_failed"
            _record_incident(
                args.output_root,
                incident_type="collector_authorization_failure",
                severity="critical",
                detail=stopped_reason,
                started=started,
            )
        elif stopped_reason == "max_reconnects_exhausted":
            exit_code = EX_UNAVAILABLE
            status = "source_unavailable"
            _record_incident(
                args.output_root,
                incident_type="collector_source_unavailable",
                severity="critical",
                detail=stopped_reason,
                started=started,
            )
        else:
            exit_code = EX_OK
            status = "stopped"
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
