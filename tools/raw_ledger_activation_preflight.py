#!/usr/bin/env python3
"""Fail-closed activation evidence for the default-off raw ledger shadow."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import shlex
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
from typing import Any, Literal

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
    load_private_collector_env_evidence,
)
from data_platform.collect.oanda import (  # noqa: E402
    CollectorConfigError,
    ENV_ACCOUNT,
    ENV_ENVIRONMENT,
    ENV_TOKEN,
    OandaConfig,
)
from data_platform.raw.backup import RawLedgerBackupError, verify_backup  # noqa: E402
from data_platform.raw.event_ledger import RawEventLedger, RawEventLedgerError  # noqa: E402
from data_platform.raw.immutable_store import ImmutableRawStore, RawStoreError  # noqa: E402
from data_platform.raw.runtime_identity import (  # noqa: E402
    COLLECTOR_ENVIRONMENT_LOCK_PATH,
    CRITICAL_COLLECTOR_PATHS,
    RuntimeIdentityError,
    critical_collector_code_sha256,
    python_runtime_isolated,
    verified_collector_environment_identity,
)
from data_platform.raw.shadow import RAW_LEDGER_FLAG, raw_ledger_enabled  # noqa: E402

PREFLIGHT_CONTRACT = "raw-ledger-activation-preflight-v1"
ALERT_EVIDENCE_CONTRACT = "raw-ledger-alert-route-evidence-v1"
CLOCK_EVIDENCE_CONTRACT = "host-clock-synchronization-evidence-v1"
ATTESTATION_CONTRACT = "raw-ledger-activation-attestation-v1"
OPENSSL_PATH = Path("/usr/bin/openssl")
GIT_PATH = Path("/usr/bin/git")
PS_PATH = Path("/bin/ps")
LSOF_PATH = Path("/usr/sbin/lsof")
Status = Literal["pass", "fail", "unknown"]


class RawLedgerPreflightError(RuntimeError):
    """Raised when an audit policy or evidence envelope is malformed."""


@dataclass(frozen=True)
class Check:
    key: str
    status: Status
    detail: str


def _aware_stamp(value: object, *, field_name: str) -> datetime:
    try:
        stamp = datetime.fromisoformat(str(value))
    except ValueError as error:
        raise RawLedgerPreflightError(f"invalid {field_name}") from error
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise RawLedgerPreflightError(f"{field_name} must be timezone-aware")
    return stamp.astimezone(UTC)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_secure_bytes(
    path: Path,
    *,
    max_bytes: int = 1024 * 1024,
    required_mode: int | None = None,
) -> bytes:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as error:
        raise RawLedgerPreflightError(
            f"cannot read evidence {path}: {type(error).__name__}"
        ) from error
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid not in (0, os.getuid())
            or stat.S_IMODE(before.st_mode) & 0o022
            or (required_mode is not None and stat.S_IMODE(before.st_mode) != required_mode)
            or before.st_size > max_bytes
        ):
            raise RawLedgerPreflightError(
                f"evidence must be owner/root-owned, non-writable, regular, and bounded: {path}"
            )
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        encoded = b"".join(chunks)
        after = os.fstat(descriptor)
        identity_before = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        )
        identity_after = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        )
        if len(encoded) > max_bytes or identity_before != identity_after:
            raise RawLedgerPreflightError(f"evidence changed while reading: {path}")
        return encoded
    finally:
        os.close(descriptor)


def _json_sha256(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _load_json_object_and_sha(path: Path) -> tuple[dict[str, Any], str]:
    encoded = _read_secure_bytes(path)
    try:
        payload = json.loads(encoded)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RawLedgerPreflightError(
            f"cannot read evidence {path}: {type(error).__name__}"
        ) from error
    if not isinstance(payload, dict):
        raise RawLedgerPreflightError(f"evidence must be a JSON object: {path}")
    return payload, hashlib.sha256(encoded).hexdigest()


def _load_json_object(path: Path) -> dict[str, Any]:
    return _load_json_object_and_sha(path)[0]


def _load_collector_env_evidence(path: Path) -> tuple[dict[str, str], str]:
    try:
        return load_private_collector_env_evidence(
            path,
            allowed_keys={ENV_TOKEN, ENV_ACCOUNT, ENV_ENVIRONMENT, RAW_LEDGER_FLAG},
        )
    except CollectorEnvironmentError as error:
        raise CollectorConfigError(str(error)) from error


def _canonical_symbol(value: str) -> str:
    symbol = value.strip().upper().replace("_", "").replace("/", "")
    if not symbol:
        raise RawLedgerPreflightError("required symbol must not be empty")
    return symbol


def _evidence_age_check(
    *,
    path: Path,
    contract: str,
    expected_hostname: str,
    now: datetime,
    max_age_seconds: float,
) -> tuple[dict[str, Any], float, str]:
    payload, digest = _load_json_object_and_sha(path)
    if payload.get("contract") != contract:
        raise RawLedgerPreflightError(f"evidence contract mismatch: {path}")
    if payload.get("hostname") != expected_hostname:
        raise RawLedgerPreflightError(f"evidence hostname mismatch: {path}")
    observed_at = _aware_stamp(payload.get("observed_at"), field_name="evidence observed_at")
    age_seconds = (now - observed_at).total_seconds()
    if age_seconds < 0:
        raise RawLedgerPreflightError(f"evidence timestamp is in the future: {path}")
    return payload, age_seconds, digest


def _nearest_existing(path: Path) -> Path:
    candidate = path.absolute()
    for current in (candidate, *candidate.parents):
        if current.exists():
            return current
    raise RawLedgerPreflightError(f"no existing ancestor for {path}")


def _directory_size(path: Path) -> int:
    total = 0
    for item in path.rglob("*"):
        item_stat = item.lstat()
        if stat.S_ISLNK(item_stat.st_mode):
            raise RawLedgerPreflightError(f"capacity path contains symlink: {item}")
        if stat.S_ISREG(item_stat.st_mode):
            total += item_stat.st_size
    return total


def _device_id(path: Path) -> int:
    return int(path.stat().st_dev)


def _verify_detached_signature_bytes(
    *, payload: bytes, signature: bytes, public_key: bytes
) -> bool:
    descriptors: list[int] = []
    try:
        for encoded in (public_key, signature, payload):
            descriptor, temporary_name = tempfile.mkstemp(prefix="fx-codex-attestation-")
            os.unlink(temporary_name)
            os.write(descriptor, encoded)
            os.lseek(descriptor, 0, os.SEEK_SET)
            descriptors.append(descriptor)
        key_fd, signature_fd, payload_fd = descriptors
        result = subprocess.run(
            [
                str(OPENSSL_PATH),
                "dgst",
                "-sha256",
                "-verify",
                f"/dev/fd/{key_fd}",
                "-signature",
                f"/dev/fd/{signature_fd}",
                f"/dev/fd/{payload_fd}",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            pass_fds=tuple(descriptors),
        )
    except (OSError, subprocess.SubprocessError):
        return False
    finally:
        for descriptor in descriptors:
            os.close(descriptor)
    return result.returncode == 0


def _signed_attestation_evidence(
    *,
    payload_path: Path,
    signature_path: Path,
    public_key_path: Path,
    expected_hostname: str,
    now: datetime,
) -> tuple[dict[str, Any], float, dict[str, str], bool]:
    payload_bytes = _read_secure_bytes(payload_path)
    signature_bytes = _read_secure_bytes(signature_path)
    public_key_bytes = _read_secure_bytes(public_key_path)
    try:
        payload = json.loads(payload_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RawLedgerPreflightError("attestation is not valid JSON") from error
    if not isinstance(payload, dict) or payload.get("contract") != ATTESTATION_CONTRACT:
        raise RawLedgerPreflightError("attestation contract mismatch")
    if payload.get("hostname") != expected_hostname:
        raise RawLedgerPreflightError("attestation hostname mismatch")
    observed_at = _aware_stamp(payload.get("observed_at"), field_name="attestation observed_at")
    age_seconds = (now - observed_at).total_seconds()
    if age_seconds < 0:
        raise RawLedgerPreflightError("attestation timestamp is in the future")
    hashes = {
        "attestation": hashlib.sha256(payload_bytes).hexdigest(),
        "signature": hashlib.sha256(signature_bytes).hexdigest(),
        "public_key": hashlib.sha256(public_key_bytes).hexdigest(),
    }
    signature_ok = _verify_detached_signature_bytes(
        payload=payload_bytes,
        signature=signature_bytes,
        public_key=public_key_bytes,
    )
    return payload, age_seconds, hashes, signature_ok


def _git_state(repo_root: Path = REPO_ROOT) -> tuple[str, bool]:
    try:
        revision = subprocess.run(
            [str(GIT_PATH), "-C", str(repo_root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout.strip()
        dirty = subprocess.run(
            [
                str(GIT_PATH),
                "-C",
                str(repo_root),
                "status",
                "--porcelain",
                "--untracked-files=normal",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError) as error:
        raise RawLedgerPreflightError("cannot establish deployed git state") from error
    return revision, not bool(dirty.strip())


def _pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (OSError, ValueError):
        return False
    return True


def _writer_process_identity(
    *,
    pid: int,
    collection_root: Path,
    env_file: Path,
    run_started_at: datetime,
) -> dict[str, Any]:
    try:
        command = subprocess.run(
            [str(PS_PATH), "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout.strip()
        started_raw = subprocess.run(
            [str(PS_PATH), "-p", str(pid), "-o", "lstart="],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout.strip()
        parts = shlex.split(command)
        process_started_local = datetime.strptime(
            " ".join(started_raw.split()), "%a %b %d %H:%M:%S %Y"
        ).replace(tzinfo=datetime.now().astimezone().tzinfo)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise RawLedgerPreflightError("cannot establish writer process identity") from error

    expected_script = (REPO_ROOT / "tools" / "fx_quote_collector.py").resolve()
    isolated_python_arguments = ("-I", "-S", "-B", "-X", "pycache_prefix=/dev/null")
    try:
        executable = Path(parts[0])
        executable_stat = executable.lstat()
        python_ok = (
            bool(parts)
            and executable.is_absolute()
            and stat.S_ISREG(executable_stat.st_mode)
            and executable_stat.st_uid in (0, os.getuid())
            and not stat.S_IMODE(executable_stat.st_mode) & 0o022
            and executable.name.lower().startswith("python")
        )
        executable_sha256 = _file_sha256(executable)
        isolated_options_ok = tuple(parts[1:6]) == isolated_python_arguments
        script_ok = len(parts) > 6 and Path(parts[6]).resolve() == expected_script
    except OSError:
        python_ok = False
        isolated_options_ok = False
        script_ok = False
        executable_sha256 = None

    lock_path = collection_root / "state" / "quote-collector.lock"
    lock_held, lock_inode = _probe_writer_lock(
        lock_path=lock_path,
        pid=pid,
        hostname=socket.gethostname(),
    )
    process_started = process_started_local.astimezone(UTC)
    start_delta = (run_started_at - process_started).total_seconds()
    critical_code = tuple(REPO_ROOT / relative for relative in CRITICAL_COLLECTOR_PATHS)
    try:
        latest_code_mtime = max(path.stat().st_mtime for path in critical_code)
        on_disk_code_sha256 = critical_collector_code_sha256(REPO_ROOT)
        approved_interpreter_sha256 = _file_sha256(
            (REPO_ROOT / ".venv" / "bin" / "python").resolve()
        )
        (
            approved_environment_sha256,
            approved_environment_lock_sha256,
        ) = verified_collector_environment_identity(
            REPO_ROOT / COLLECTOR_ENVIRONMENT_LOCK_PATH,
            environment_root=VENV_ROOT,
        )
    except (OSError, RuntimeIdentityError) as error:
        raise RawLedgerPreflightError("cannot bind collector code image") from error
    approved_venv = Path(sys.executable).absolute() == (VENV_ROOT / "bin" / "python").absolute()
    code_deployed_before_process_start = latest_code_mtime <= process_started.timestamp()
    identity_ok = (
        python_ok
        and isolated_options_ok
        and script_ok
        and _single_path_option_matches(parts, "--env-file", env_file)
        and _single_path_option_matches(parts, "--output-root", collection_root)
        and lock_held
        and 0 <= start_delta <= 120
        and code_deployed_before_process_start
        and approved_venv
    )
    return {
        "ok": identity_ok,
        "command_sha256": hashlib.sha256(command.encode("utf-8")).hexdigest(),
        "executable_sha256": executable_sha256,
        "process_started_at": process_started.isoformat(),
        "lock_inode": lock_inode,
        "on_disk_code_sha256": on_disk_code_sha256,
        "approved_interpreter_sha256": approved_interpreter_sha256,
        "approved_environment_sha256": approved_environment_sha256,
        "approved_environment_lock_sha256": approved_environment_lock_sha256,
        "approved_venv": approved_venv,
        "isolated_options_ok": isolated_options_ok,
        "latest_code_mtime": latest_code_mtime,
        "code_deployed_before_process_start": code_deployed_before_process_start,
        "start_delta_seconds": start_delta,
    }


def _single_path_option_matches(parts: list[str], name: str, expected: Path) -> bool:
    """Match exactly one argparse path option; later duplicate values must not win."""

    indices = [index for index, value in enumerate(parts) if value == name]
    if len(indices) != 1 or indices[0] + 1 >= len(parts):
        return False
    return Path(parts[indices[0] + 1]).absolute() == expected.absolute()


def _probe_writer_lock(*, lock_path: Path, pid: int, hostname: str) -> tuple[bool, int | None]:
    try:
        lock_stat = lock_path.lstat()
        if (
            stat.S_ISLNK(lock_stat.st_mode)
            or not stat.S_ISREG(lock_stat.st_mode)
            or lock_stat.st_uid != os.getuid()
            or stat.S_IMODE(lock_stat.st_mode) & 0o022
        ):
            return False, None
        metadata = _load_json_object(lock_path)
        lsof_output = subprocess.run(
            [str(LSOF_PATH), "-Fpf", "--", str(lock_path)],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        ).stdout.splitlines()
        open_pids = {
            int(line[1:]) for line in lsof_output if line.startswith("p") and line[1:].isdigit()
        }
        descriptor = os.open(lock_path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                unavailable = True
            else:
                unavailable = False
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)
    except (OSError, RawLedgerPreflightError, ValueError):
        return False, None
    proven = (
        unavailable
        and open_pids == {pid}
        and metadata.get("pid") == pid
        and metadata.get("host") == hostname
        and metadata.get("name") == "quote-collector"
    )
    return proven, lock_stat.st_ino


def _live_ledger_evidence(
    *,
    collection_root: Path,
    required_symbols: tuple[str, ...],
    now: datetime,
    stale_after_seconds: float,
    min_events_per_symbol: int,
    expected_account_id_sha256: str,
    max_source_time_lead_seconds: float,
) -> dict[str, Any]:
    database = collection_root / "raw-ledger" / "events.sqlite3"
    connection = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)
    raw_store = ImmutableRawStore(collection_root / "raw")
    counts = {symbol: 0 for symbol in required_symbols}
    latest: dict[str, datetime] = {}
    ingest_times: dict[str, list[str]] = {symbol: [] for symbol in required_symbols}
    events: dict[str, list[dict[str, str]]] = {symbol: [] for symbol in required_symbols}
    try:
        annotation_count = int(
            connection.execute("SELECT COUNT(*) FROM event_annotations").fetchone()[0]
        )
        for row in connection.execute("SELECT metadata_json FROM raw_events"):
            metadata = json.loads(str(row[0]))
            provenance = metadata.get("provenance")
            if not isinstance(provenance, dict):
                continue
            symbol = _canonical_symbol(str(metadata.get("symbol", "")))
            if symbol not in counts:
                continue
            try:
                payload_hash = str(metadata.get("payload_hash"))
                payload = json.loads(raw_store.get(payload_hash))
                event_time = _aware_stamp(
                    metadata.get("event_time"), field_name="OANDA live event_time"
                )
                receive_time = _aware_stamp(
                    metadata.get("receive_time"), field_name="OANDA live receive_time"
                )
                ingested_at = _aware_stamp(
                    metadata.get("ingest_time"), field_name="OANDA live ingest_time"
                )
                payload_time = _aware_stamp(payload.get("time"), field_name="OANDA payload time")
                bids = payload.get("bids")
                asks = payload.get("asks")
                bid = float(bids[0]["price"])
                ask = float(asks[0]["price"])
                connection_id = str(provenance.get("connection_id", ""))
                expected_scope = (
                    f"pricing_stream:live:account:{expected_account_id_sha256}:"
                    "event_type:instrument:event_time:v1"
                )
                expected_source_id = hashlib.sha256(
                    f"price|{symbol}|{event_time.isoformat()}".encode()
                ).hexdigest()
                expected_event_id = hashlib.sha256(
                    "|".join(
                        (
                            "oanda-pricing-stream",
                            connection_id,
                            receive_time.isoformat(),
                            payload_hash,
                        )
                    ).encode("utf-8")
                ).hexdigest()
            except (
                IndexError,
                KeyError,
                RawLedgerPreflightError,
                RawStoreError,
                TypeError,
                ValueError,
            ):
                continue
            if not (
                isinstance(payload, dict)
                and payload.get("type") == "PRICE"
                and _canonical_symbol(str(payload.get("instrument", ""))) == symbol
                and isinstance(bids, list)
                and isinstance(asks, list)
                and bids
                and asks
                and math.isfinite(bid)
                and math.isfinite(ask)
                and 0 < bid <= ask
                and payload_time == event_time
                and event_time <= receive_time + timedelta(seconds=max_source_time_lead_seconds)
                and receive_time - event_time <= timedelta(seconds=stale_after_seconds)
                and receive_time <= ingested_at <= now
                and metadata.get("source") == "oanda-pricing-stream"
                and metadata.get("event_type") == "price"
                and metadata.get("source_event_id") == expected_source_id
                and metadata.get("event_id") == expected_event_id
                and provenance.get("writer") == "fx_quote_collector"
                and provenance.get("account_id_sha256") == expected_account_id_sha256
                and provenance.get("account_environment") == "live"
                and provenance.get("collection_mode") == "live_stream"
                and provenance.get("source_endpoint_class") == "streaming_pricing"
                and provenance.get("source_event_id_scope") == expected_scope
                and connection_id
            ):
                continue
            counts[symbol] += 1
            ingest_times[symbol].append(ingested_at.isoformat())
            events[symbol].append(
                {
                    "event_id": str(metadata["event_id"]),
                    "ingest_time": ingested_at.isoformat(),
                }
            )
            if symbol not in latest or ingested_at > latest[symbol]:
                latest[symbol] = ingested_at
    except (sqlite3.Error, json.JSONDecodeError, RawLedgerPreflightError) as error:
        raise RawLedgerPreflightError(
            f"cannot verify OANDA live ledger evidence: {type(error).__name__}"
        ) from error
    finally:
        connection.close()
    ages = {
        symbol: (now - latest[symbol]).total_seconds()
        for symbol in required_symbols
        if symbol in latest
    }
    return {
        "coverage_ok": all(
            counts[symbol] >= min_events_per_symbol
            and 0 <= ages.get(symbol, math.inf) <= stale_after_seconds
            for symbol in required_symbols
        ),
        "counts": counts,
        "age_seconds": ages,
        "ingest_times": ingest_times,
        "events": events,
        "annotation_count": annotation_count,
    }


def _trial_ledger_summary(
    *,
    live_evidence: dict[str, Any],
    symbols: tuple[str, ...],
    started_at: datetime,
    ended_at: datetime,
) -> dict[str, Any]:
    event_ids: list[str] = []
    counts: dict[str, int] = {}
    for symbol in symbols:
        selected: list[str] = []
        for event in live_evidence["events"].get(symbol, []):
            stamp = _aware_stamp(event.get("ingest_time"), field_name="trial ingest_time")
            if started_at <= stamp <= ended_at:
                selected.append(str(event.get("event_id")))
        counts[symbol] = len(selected)
        event_ids.extend(selected)
    digest = hashlib.sha256(("\n".join(sorted(event_ids)) + "\n").encode()).hexdigest()
    return {"symbol_counts": counts, "event_ids_sha256": digest}


def _finite_number(value: object, *, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise RawLedgerPreflightError(f"{field_name} must be a finite number")
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise RawLedgerPreflightError(f"{field_name} must be a finite number") from error
    if not math.isfinite(number):
        raise RawLedgerPreflightError(f"{field_name} must be a finite number")
    return number


def _validate_policy(
    *,
    stale_after_seconds: float,
    max_duplicate_ratio: float,
    max_quarantine_ratio: float,
    max_raw_failure_ratio: float,
    max_raw_queue_overflow_ratio: float,
    max_annotation_failure_ratio: float,
    max_annotation_queue_overflow_ratio: float,
    max_raw_attempt_rate_per_second: float,
    max_queue_backlog: int,
    min_shadow_observation_seconds: float,
    min_raw_attempts: int,
    min_live_events_per_symbol: int,
    min_free_bytes: int,
    min_backup_free_bytes: int,
    retention_days: int,
    backup_retention_snapshots: int,
    max_daily_growth_bytes: int,
    max_backup_age_seconds: float,
    max_backup_lag_events: int,
    max_backup_lag_annotations: int,
    max_alert_evidence_age_seconds: float,
    max_clock_evidence_age_seconds: float,
    max_clock_offset_ms: float,
) -> None:
    for name, value in (
        ("stale_after_seconds", stale_after_seconds),
        ("max_backup_age_seconds", max_backup_age_seconds),
        ("max_alert_evidence_age_seconds", max_alert_evidence_age_seconds),
        ("max_clock_evidence_age_seconds", max_clock_evidence_age_seconds),
        ("max_clock_offset_ms", max_clock_offset_ms),
        ("min_shadow_observation_seconds", min_shadow_observation_seconds),
        ("max_raw_attempt_rate_per_second", max_raw_attempt_rate_per_second),
    ):
        if not math.isfinite(value) or value < 0:
            raise RawLedgerPreflightError(f"{name} must be finite and non-negative")
    for name, value in (
        ("max_duplicate_ratio", max_duplicate_ratio),
        ("max_quarantine_ratio", max_quarantine_ratio),
        ("max_raw_failure_ratio", max_raw_failure_ratio),
        ("max_raw_queue_overflow_ratio", max_raw_queue_overflow_ratio),
        ("max_annotation_failure_ratio", max_annotation_failure_ratio),
        ("max_annotation_queue_overflow_ratio", max_annotation_queue_overflow_ratio),
    ):
        if not math.isfinite(value) or not 0 <= value <= 1:
            raise RawLedgerPreflightError(f"{name} must be between 0 and 1")
    for name, value in (
        ("max_queue_backlog", max_queue_backlog),
        ("min_free_bytes", min_free_bytes),
        ("min_backup_free_bytes", min_backup_free_bytes),
        ("retention_days", retention_days),
        ("backup_retention_snapshots", backup_retention_snapshots),
        ("max_daily_growth_bytes", max_daily_growth_bytes),
        ("max_backup_lag_events", max_backup_lag_events),
        ("max_backup_lag_annotations", max_backup_lag_annotations),
        ("min_raw_attempts", min_raw_attempts),
        ("min_live_events_per_symbol", min_live_events_per_symbol),
    ):
        if isinstance(value, bool) or value < 0:
            raise RawLedgerPreflightError(f"{name} must be non-negative")
    if (
        retention_days < 1
        or backup_retention_snapshots < 1
        or max_daily_growth_bytes < 1
        or min_raw_attempts < 1
        or min_live_events_per_symbol < 1
    ):
        raise RawLedgerPreflightError(
            "retention_days, growth, raw attempts, and per-symbol events must be positive"
        )


def audit_preflight(
    *,
    collection_root: Path,
    env_file: Path,
    expected_hostname: str,
    expected_oanda_environment: str,
    required_symbols: tuple[str, ...],
    stale_after_seconds: float,
    max_duplicate_ratio: float,
    max_quarantine_ratio: float,
    max_raw_failure_ratio: float,
    max_raw_queue_overflow_ratio: float,
    max_annotation_failure_ratio: float,
    max_annotation_queue_overflow_ratio: float,
    max_raw_attempt_rate_per_second: float,
    max_queue_backlog: int,
    min_shadow_observation_seconds: float,
    min_raw_attempts: int,
    min_live_events_per_symbol: int,
    min_free_bytes: int,
    min_backup_free_bytes: int,
    retention_days: int,
    backup_retention_snapshots: int,
    max_daily_growth_bytes: int,
    backup_root: Path,
    max_backup_age_seconds: float,
    max_backup_lag_events: int,
    max_backup_lag_annotations: int,
    alert_evidence: Path,
    max_alert_evidence_age_seconds: float,
    clock_evidence: Path,
    max_clock_evidence_age_seconds: float,
    max_clock_offset_ms: float,
    attestation: Path,
    attestation_signature: Path,
    attestation_public_key: Path,
    expected_attestation_public_key_sha256: str,
    max_attestation_age_seconds: float,
    expected_deployed_sha: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Evaluate readiness without mutating services, source data, or configuration."""

    _validate_policy(
        stale_after_seconds=stale_after_seconds,
        max_duplicate_ratio=max_duplicate_ratio,
        max_quarantine_ratio=max_quarantine_ratio,
        max_raw_failure_ratio=max_raw_failure_ratio,
        max_raw_queue_overflow_ratio=max_raw_queue_overflow_ratio,
        max_annotation_failure_ratio=max_annotation_failure_ratio,
        max_annotation_queue_overflow_ratio=max_annotation_queue_overflow_ratio,
        max_raw_attempt_rate_per_second=max_raw_attempt_rate_per_second,
        max_queue_backlog=max_queue_backlog,
        min_shadow_observation_seconds=min_shadow_observation_seconds,
        min_raw_attempts=min_raw_attempts,
        min_live_events_per_symbol=min_live_events_per_symbol,
        min_free_bytes=min_free_bytes,
        min_backup_free_bytes=min_backup_free_bytes,
        retention_days=retention_days,
        backup_retention_snapshots=backup_retention_snapshots,
        max_daily_growth_bytes=max_daily_growth_bytes,
        max_backup_age_seconds=max_backup_age_seconds,
        max_backup_lag_events=max_backup_lag_events,
        max_backup_lag_annotations=max_backup_lag_annotations,
        max_alert_evidence_age_seconds=max_alert_evidence_age_seconds,
        max_clock_evidence_age_seconds=max_clock_evidence_age_seconds,
        max_clock_offset_ms=max_clock_offset_ms,
    )
    observed_at = _aware_stamp(now or datetime.now(UTC), field_name="now")
    if expected_oanda_environment != "live":
        raise RawLedgerPreflightError("activation evidence requires OANDA environment live")
    if not math.isfinite(max_attestation_age_seconds) or max_attestation_age_seconds < 0:
        raise RawLedgerPreflightError("max_attestation_age_seconds must be finite and non-negative")
    if not (
        len(expected_attestation_public_key_sha256) == 64
        and all(
            character in "0123456789abcdef" for character in expected_attestation_public_key_sha256
        )
    ):
        raise RawLedgerPreflightError(
            "expected_attestation_public_key_sha256 must be lowercase SHA-256"
        )
    if not (
        len(expected_deployed_sha) == 40
        and all(character in "0123456789abcdef" for character in expected_deployed_sha)
    ):
        raise RawLedgerPreflightError("expected_deployed_sha must be a lowercase Git SHA")
    symbols = tuple(sorted({_canonical_symbol(value) for value in required_symbols}))
    if not symbols:
        raise RawLedgerPreflightError("at least one required symbol is mandatory")
    checks: list[Check] = []

    policy: dict[str, Any] = {
        "stale_after_seconds": stale_after_seconds,
        "max_duplicate_ratio": max_duplicate_ratio,
        "max_quarantine_ratio": max_quarantine_ratio,
        "max_raw_failure_ratio": max_raw_failure_ratio,
        "max_raw_queue_overflow_ratio": max_raw_queue_overflow_ratio,
        "max_annotation_failure_ratio": max_annotation_failure_ratio,
        "max_annotation_queue_overflow_ratio": max_annotation_queue_overflow_ratio,
        "max_raw_attempt_rate_per_second": max_raw_attempt_rate_per_second,
        "max_queue_backlog": max_queue_backlog,
        "min_shadow_observation_seconds": min_shadow_observation_seconds,
        "min_raw_attempts": min_raw_attempts,
        "min_live_events_per_symbol": min_live_events_per_symbol,
        "min_free_bytes": min_free_bytes,
        "min_backup_free_bytes": min_backup_free_bytes,
        "retention_days": retention_days,
        "backup_retention_snapshots": backup_retention_snapshots,
        "max_daily_growth_bytes": max_daily_growth_bytes,
        "max_backup_age_seconds": max_backup_age_seconds,
        "max_backup_lag_events": max_backup_lag_events,
        "max_backup_lag_annotations": max_backup_lag_annotations,
        "max_alert_evidence_age_seconds": max_alert_evidence_age_seconds,
        "max_clock_evidence_age_seconds": max_clock_evidence_age_seconds,
        "max_clock_offset_ms": max_clock_offset_ms,
        "max_attestation_age_seconds": max_attestation_age_seconds,
        "expected_deployed_sha": expected_deployed_sha,
    }
    policy_sha256 = _json_sha256(policy)

    def add(key: str, passed: bool, detail: str, *, unknown: bool = False) -> None:
        checks.append(Check(key, "unknown" if unknown else "pass" if passed else "fail", detail))

    observed_hostname = socket.gethostname()
    add(
        "expected_host",
        observed_hostname == expected_hostname,
        f"observed={observed_hostname} expected={expected_hostname}",
    )

    try:
        deployed_sha, worktree_clean = _git_state()
        release_ok = deployed_sha == expected_deployed_sha and worktree_clean
        add(
            "deployed_release_state",
            release_ok,
            (
                f"deployed_sha={deployed_sha} expected_sha={expected_deployed_sha} "
                f"worktree_clean={worktree_clean}"
            ),
        )
    except RawLedgerPreflightError as error:
        deployed_sha = None
        worktree_clean = False
        add("deployed_release_state", False, type(error).__name__, unknown=True)

    env_sha256: str | None = None
    expected_account_id_sha256: str | None = None
    try:
        values, env_sha256 = _load_collector_env_evidence(env_file)
        collector_config = OandaConfig.from_env(values)
        expected_account_id_sha256 = hashlib.sha256(
            collector_config.account_id.encode("utf-8")
        ).hexdigest()
        add("collector_config", True, "mode-0600 narrow env parsed; values withheld")
        add(
            "oanda_environment",
            collector_config.environment == expected_oanda_environment,
            (f"observed={collector_config.environment} " f"expected={expected_oanda_environment}"),
        )
        add(
            "shadow_flag_enabled",
            raw_ledger_enabled(values),
            f"enabled={raw_ledger_enabled(values)}",
        )
    except (CollectorConfigError, OSError, RawLedgerPreflightError) as error:
        add("collector_config", False, type(error).__name__)
        add("oanda_environment", False, "collector configuration unavailable", unknown=True)
        add("shadow_flag_enabled", False, "collector configuration unavailable", unknown=True)

    root_exists = False
    try:
        root_stat = collection_root.lstat()
        root_exists = True
        root_secure = (
            stat.S_ISDIR(root_stat.st_mode)
            and not stat.S_ISLNK(root_stat.st_mode)
            and root_stat.st_uid == os.getuid()
            and not stat.S_IMODE(root_stat.st_mode) & 0o077
        )
        add(
            "collection_root_permissions",
            root_secure,
            f"mode={stat.S_IMODE(root_stat.st_mode):03o} user_owned={root_stat.st_uid == os.getuid()}",
        )
    except OSError as error:
        add("collection_root_permissions", False, type(error).__name__, unknown=True)

    try:
        if not root_exists:
            raise RawLedgerPreflightError("collection root missing")
        current_collection_bytes = _directory_size(collection_root)
        add(
            "collection_size_inventory",
            current_collection_bytes > 0,
            f"current_collection_bytes={current_collection_bytes}",
        )
    except (OSError, RawLedgerPreflightError) as error:
        current_collection_bytes = None
        add("collection_size_inventory", False, type(error).__name__, unknown=True)

    capacity_path = collection_root if root_exists else _nearest_existing(collection_root)
    disk = shutil.disk_usage(capacity_path)
    projected_retention_bytes = retention_days * max_daily_growth_bytes
    required_free_bytes = min_free_bytes + projected_retention_bytes
    add(
        "retention_capacity",
        disk.free >= required_free_bytes,
        (
            f"free={disk.free} required={required_free_bytes} min_headroom={min_free_bytes} "
            f"retention_days={retention_days} daily_cap={max_daily_growth_bytes}"
        ),
    )

    live_health: dict[str, Any] | None = None
    live_evidence: dict[str, Any] | None = None
    if root_exists:
        try:
            live_health = RawEventLedger(
                collection_root / "raw-ledger",
                blob_store=ImmutableRawStore(collection_root / "raw"),
            ).health_snapshot(
                now=observed_at,
                stale_after_seconds=stale_after_seconds,
                max_duplicate_ratio=max_duplicate_ratio,
                max_quarantine_ratio=max_quarantine_ratio,
                required_symbols=symbols,
            )
            add(
                "raw_ledger_health",
                live_health.get("status") == "ok",
                f"status={live_health.get('status')} reasons={live_health.get('reasons')}",
            )
            live_evidence = _live_ledger_evidence(
                collection_root=collection_root,
                required_symbols=symbols,
                now=observed_at,
                stale_after_seconds=stale_after_seconds,
                min_events_per_symbol=min_live_events_per_symbol,
                expected_account_id_sha256=(expected_account_id_sha256 or ""),
                max_source_time_lead_seconds=max_clock_offset_ms / 1000,
            )
            add(
                "oanda_live_stream_coverage",
                live_evidence["coverage_ok"] is True,
                f"counts={live_evidence['counts']} ages={live_evidence['age_seconds']}",
            )
        except (OSError, RawEventLedgerError, RawLedgerPreflightError, ValueError) as error:
            add("raw_ledger_health", False, type(error).__name__)
            add("oanda_live_stream_coverage", False, type(error).__name__)
    else:
        add("raw_ledger_health", False, "collection root missing", unknown=True)
        add("oanda_live_stream_coverage", False, "collection root missing", unknown=True)

    metrics_path = collection_root / "state" / "raw_ledger_shadow_metrics.json"
    metrics_summary: dict[str, Any] | None = None
    try:
        metrics, metrics_file_sha256 = _load_json_object_and_sha(metrics_path)
        metrics_at = _aware_stamp(metrics.get("observed_at"), field_name="metrics observed_at")
        run_started_at = _aware_stamp(
            metrics.get("run_started_at"), field_name="metrics run_started_at"
        )
        metrics_age = (observed_at - metrics_at).total_seconds()
        if metrics_age < 0:
            raise RawLedgerPreflightError("metrics observed_at is in the future")
        observation_seconds = (metrics_at - run_started_at).total_seconds()
        if observation_seconds < 0:
            raise RawLedgerPreflightError("metrics run_started_at is after observed_at")
        counter_names = (
            "attempted",
            "queued",
            "stored",
            "idempotent",
            "failed",
            "queue_overflow",
            "raw_attempted",
            "raw_queued",
            "raw_stored",
            "raw_idempotent",
            "raw_failed",
            "raw_queue_overflow",
            "annotations_attempted",
            "annotations_queued",
            "annotations_stored",
            "annotations_idempotent",
            "annotations_failed",
            "annotations_queue_overflow",
            "failure_reports_queued",
        )
        counters = {name: int(metrics.get(name, -1)) for name in counter_names}
        raw_attempted = counters["raw_attempted"]
        raw_failed = counters["raw_failed"]
        raw_queue_overflow = counters["raw_queue_overflow"]
        writer_pid = int(metrics.get("writer_pid", -1))
        writer_identity = _writer_process_identity(
            pid=writer_pid,
            collection_root=collection_root,
            env_file=env_file,
            run_started_at=run_started_at,
        )
        runtime_on_disk_code_sha256 = metrics.get("runtime_on_disk_code_sha256")
        runtime_collector_env_sha256 = metrics.get("collector_env_sha256")
        runtime_interpreter_sha256 = metrics.get("runtime_interpreter_sha256")
        runtime_environment_sha256 = metrics.get("runtime_environment_sha256")
        runtime_environment_lock_sha256 = metrics.get("runtime_environment_lock_sha256")
        runtime_isolated = metrics.get("runtime_isolated") is True
        runtime_identity_ok = (
            runtime_on_disk_code_sha256 == writer_identity["on_disk_code_sha256"]
            and runtime_collector_env_sha256 == env_sha256
            and runtime_interpreter_sha256 == writer_identity["approved_interpreter_sha256"]
            and runtime_environment_sha256 == writer_identity["approved_environment_sha256"]
            and runtime_environment_lock_sha256
            == writer_identity["approved_environment_lock_sha256"]
            and runtime_isolated
        )
        raw_failure_ratio = _finite_number(
            metrics.get("raw_failure_ratio"), field_name="raw_failure_ratio"
        )
        raw_overflow_ratio = _finite_number(
            metrics.get("raw_queue_overflow_ratio"),
            field_name="raw_queue_overflow_ratio",
        )
        annotation_failure_ratio = _finite_number(
            metrics.get("annotation_failure_ratio"),
            field_name="annotation_failure_ratio",
        )
        annotation_overflow_ratio = _finite_number(
            metrics.get("annotation_queue_overflow_ratio"),
            field_name="annotation_queue_overflow_ratio",
        )
        queue_backlog = int(metrics.get("queue_backlog", -1))
        failure_queue_backlog = int(metrics.get("failure_queue_backlog", -1))
        unfinished_tasks = int(metrics.get("unfinished_tasks", -1))
        failure_unfinished_tasks = int(metrics.get("failure_unfinished_tasks", -1))
        live_worker = metrics.get("final") is False and metrics.get("worker_alive") is True
        failure_worker_ok = (
            counters["failure_reports_queued"] == 0 or metrics.get("failure_worker_alive") is True
        )
        diagnostics_ok = all(
            int(metrics.get(key, -1)) == 0
            for key in (
                "failure_report_queue_overflow",
                "failure_report_errors",
                "failure_log_write_errors",
                "reporter_write_failures",
            )
        )
        recomputed_failure_ratio = (
            raw_failed / raw_attempted if raw_attempted > 0 and raw_failed >= 0 else math.inf
        )
        recomputed_overflow_ratio = (
            raw_queue_overflow / raw_attempted
            if raw_attempted > 0 and raw_queue_overflow >= 0
            else math.inf
        )
        annotation_attempted = counters["annotations_attempted"]
        recomputed_annotation_failure_ratio = (
            counters["annotations_failed"] / annotation_attempted
            if annotation_attempted > 0 and counters["annotations_failed"] >= 0
            else 0.0 if annotation_attempted == 0 else math.inf
        )
        recomputed_annotation_overflow_ratio = (
            counters["annotations_queue_overflow"] / annotation_attempted
            if annotation_attempted > 0 and counters["annotations_queue_overflow"] >= 0
            else 0.0 if annotation_attempted == 0 else math.inf
        )
        ratios_match_counters = all(
            (
                math.isclose(raw_failure_ratio, recomputed_failure_ratio, abs_tol=1e-15),
                math.isclose(raw_overflow_ratio, recomputed_overflow_ratio, abs_tol=1e-15),
                math.isclose(
                    annotation_failure_ratio,
                    recomputed_annotation_failure_ratio,
                    abs_tol=1e-15,
                ),
                math.isclose(
                    annotation_overflow_ratio,
                    recomputed_annotation_overflow_ratio,
                    abs_tol=1e-15,
                ),
            )
        )
        counter_arithmetic_ok = (
            all(value >= 0 for value in counters.values())
            and counters["raw_attempted"] == counters["raw_queued"] + counters["raw_queue_overflow"]
            and counters["raw_queued"]
            == counters["raw_stored"]
            + counters["raw_idempotent"]
            + counters["raw_failed"]
            - counters["raw_queue_overflow"]
            and counters["annotations_attempted"]
            == counters["annotations_queued"] + counters["annotations_queue_overflow"]
            and counters["annotations_queued"]
            == counters["annotations_stored"]
            + counters["annotations_idempotent"]
            + counters["annotations_failed"]
            - counters["annotations_queue_overflow"]
            and counters["attempted"]
            == counters["raw_attempted"] + counters["annotations_attempted"]
            and counters["queued"] == counters["raw_queued"] + counters["annotations_queued"]
            and counters["stored"] == counters["raw_stored"] + counters["annotations_stored"]
            and counters["idempotent"]
            == counters["raw_idempotent"] + counters["annotations_idempotent"]
            and counters["failed"] == counters["raw_failed"] + counters["annotations_failed"]
            and counters["queue_overflow"]
            == counters["raw_queue_overflow"] + counters["annotations_queue_overflow"]
        )
        metrics_ok = (
            metrics.get("enabled") is True
            and 0 <= metrics_age <= stale_after_seconds
            and observation_seconds >= min_shadow_observation_seconds
            and raw_attempted >= min_raw_attempts
            and raw_failure_ratio <= max_raw_failure_ratio
            and raw_overflow_ratio <= max_raw_queue_overflow_ratio
            and annotation_failure_ratio <= max_annotation_failure_ratio
            and annotation_overflow_ratio <= max_annotation_queue_overflow_ratio
            and queue_backlog == 0
            and failure_queue_backlog == 0
            and unfinished_tasks == 0
            and failure_unfinished_tasks == 0
            and live_worker
            and failure_worker_ok
            and _pid_exists(writer_pid)
            and writer_identity["ok"] is True
            and runtime_identity_ok
            and diagnostics_ok
            and ratios_match_counters
            and counter_arithmetic_ok
        )
        metrics_summary = {
            "run_started_at": run_started_at.isoformat(),
            "observed_at": metrics_at.isoformat(),
            "raw_attempted": raw_attempted,
            "raw_failed": raw_failed,
            "raw_queue_overflow": raw_queue_overflow,
            "counter_snapshot": counters,
            "writer_pid": writer_pid,
            "file_sha256": metrics_file_sha256,
            "process_command_sha256": writer_identity["command_sha256"],
            "process_executable_sha256": writer_identity["executable_sha256"],
            "process_started_at": writer_identity["process_started_at"],
            "lock_inode": writer_identity["lock_inode"],
            "on_disk_code_sha256": writer_identity["on_disk_code_sha256"],
            "collector_env_sha256": runtime_collector_env_sha256,
            "runtime_interpreter_sha256": runtime_interpreter_sha256,
            "runtime_environment_sha256": runtime_environment_sha256,
            "runtime_environment_lock_sha256": runtime_environment_lock_sha256,
            "runtime_isolated": runtime_isolated,
        }
        add(
            "shadow_runtime_metrics",
            metrics_ok,
            (
                f"age={metrics_age:.3f}s observation={observation_seconds:.3f}s "
                f"raw_attempted={raw_attempted} "
                f"failure_ratio={raw_failure_ratio} overflow_ratio={raw_overflow_ratio} "
                f"annotation_failure_ratio={annotation_failure_ratio} "
                f"annotation_overflow_ratio={annotation_overflow_ratio} "
                f"backlog={queue_backlog}/{failure_queue_backlog} "
                f"unfinished={unfinished_tasks}/{failure_unfinished_tasks} "
                f"live_worker={live_worker} writer_pid_alive={_pid_exists(writer_pid)} "
                f"writer_identity={writer_identity['ok']} "
                f"runtime_identity={runtime_identity_ok} "
                f"diagnostics_ok={diagnostics_ok} "
                f"ratios_match_counters={ratios_match_counters} "
                f"counter_arithmetic_ok={counter_arithmetic_ok}"
            ),
        )
    except (OSError, ValueError, TypeError, RawLedgerPreflightError) as error:
        add(
            "shadow_runtime_metrics", False, type(error).__name__, unknown=not metrics_path.exists()
        )

    if live_evidence is not None and metrics_summary is not None:
        trial_started_at = _aware_stamp(
            metrics_summary["run_started_at"], field_name="trial run_started_at"
        )
        trial_ended_at = _aware_stamp(
            metrics_summary["observed_at"], field_name="trial observed_at"
        )
        trial_summary = _trial_ledger_summary(
            live_evidence=live_evidence,
            symbols=symbols,
            started_at=trial_started_at,
            ended_at=trial_ended_at,
        )
        metrics_summary["trial_ledger"] = trial_summary
        trial_counts = trial_summary["symbol_counts"]
        add(
            "trial_live_event_volume",
            all(count >= min_live_events_per_symbol for count in trial_counts.values()),
            f"counts_since_run_start={trial_counts} minimum={min_live_events_per_symbol}",
        )
    else:
        add(
            "trial_live_event_volume",
            False,
            "live ledger or runtime metrics unavailable",
            unknown=True,
        )

    backup_result: dict[str, Any] | None = None
    try:
        backup_result = verify_backup(backup_root, now=observed_at)
        backup_health = backup_result.get("restore_health")
        backup_symbol_counts = (
            backup_health.get("symbol_counts") if isinstance(backup_health, dict) else {}
        )
        backup_symbol_ages = (
            backup_health.get("symbol_age_seconds") if isinstance(backup_health, dict) else {}
        )
        backup_symbols_ok = all(
            isinstance(backup_symbol_counts, dict)
            and int(backup_symbol_counts.get(symbol, 0)) > 0
            and isinstance(backup_symbol_ages, dict)
            and 0 <= float(backup_symbol_ages.get(symbol, math.inf)) <= stale_after_seconds
            for symbol in symbols
        )
        live_total = int(live_health.get("total_events", 0)) if live_health else 0
        backup_total = int(backup_result.get("event_count", 0))
        event_lag = live_total - backup_total
        live_annotations = int(live_evidence.get("annotation_count", 0)) if live_evidence else 0
        backup_annotations = int(backup_result.get("annotation_count", 0))
        annotation_lag = live_annotations - backup_annotations
        live_device = _device_id(collection_root)
        backup_device = _device_id(backup_root)
        current_source_hash = hashlib.sha256(
            str(collection_root.absolute()).encode("utf-8")
        ).hexdigest()
        source_binding_ok = (
            backup_result.get("source_collection_root_sha256") == current_source_hash
        )
        backup_ok = (
            backup_result.get("ok") is True
            and backup_result.get("source_hostname") == expected_hostname
            and live_device != backup_device
            and source_binding_ok
            and float(backup_result.get("age_seconds", math.inf)) <= max_backup_age_seconds
            and 0 <= event_lag <= max_backup_lag_events
            and 0 <= annotation_lag <= max_backup_lag_annotations
            and backup_symbols_ok
        )
        add(
            "backup_restore",
            backup_ok,
            (
                f"age={backup_result.get('age_seconds')}s separate_device={live_device != backup_device} "
                f"source_binding={source_binding_ok} event_lag={event_lag} "
                f"annotation_lag={annotation_lag} "
                f"symbols_ok={backup_symbols_ok}"
            ),
        )
    except (OSError, RawLedgerBackupError, ValueError, TypeError) as error:
        add("backup_restore", False, type(error).__name__, unknown=not backup_root.exists())

    projected_backup_series_bytes = (
        None
        if current_collection_bytes is None
        else backup_retention_snapshots * current_collection_bytes
        + max_daily_growth_bytes
        * backup_retention_snapshots
        * (backup_retention_snapshots + 1)
        // 2
    )
    required_backup_free_bytes = (
        None
        if projected_backup_series_bytes is None
        else min_backup_free_bytes + projected_backup_series_bytes
    )
    try:
        if required_backup_free_bytes is None:
            raise RawLedgerPreflightError("collection size unavailable")
        backup_disk = shutil.disk_usage(_nearest_existing(backup_root))
        add(
            "backup_retention_capacity",
            backup_disk.free >= required_backup_free_bytes,
            (
                f"free={backup_disk.free} required={required_backup_free_bytes} "
                f"min_headroom={min_backup_free_bytes} "
                f"full_snapshots={backup_retention_snapshots} "
                f"projected_series={projected_backup_series_bytes}"
            ),
        )
    except (OSError, RawLedgerPreflightError) as error:
        backup_disk = None
        add("backup_retention_capacity", False, type(error).__name__, unknown=True)

    collection_root_sha256 = hashlib.sha256(
        str(collection_root.absolute()).encode("utf-8")
    ).hexdigest()
    evidence_hashes: dict[str, str | None] = {
        "backup_manifest": (str(backup_result.get("manifest_sha256")) if backup_result else None),
        "alert": None,
        "clock": None,
        "collector_env": env_sha256,
        "metrics": (
            str(metrics_summary.get("file_sha256")) if metrics_summary is not None else None
        ),
        "policy": policy_sha256,
        "attestation": None,
        "attestation_signature": None,
        "attestation_public_key": None,
    }
    try:
        alert, alert_age, alert_sha = _evidence_age_check(
            path=alert_evidence,
            contract=ALERT_EVIDENCE_CONTRACT,
            expected_hostname=expected_hostname,
            now=observed_at,
            max_age_seconds=max_alert_evidence_age_seconds,
        )
        alert_ok = (
            alert_age <= max_alert_evidence_age_seconds
            and alert.get("route_configured") is True
            and alert.get("delivery_test_passed") is True
            and alert.get("raw_ledger_health_routed") is True
        )
        add("freshness_alert_routing", alert_ok, f"age={alert_age:.3f}s")
        evidence_hashes["alert"] = alert_sha
    except RawLedgerPreflightError as error:
        add(
            "freshness_alert_routing",
            False,
            type(error).__name__,
            unknown=not alert_evidence.exists(),
        )

    try:
        clock, clock_age, clock_sha = _evidence_age_check(
            path=clock_evidence,
            contract=CLOCK_EVIDENCE_CONTRACT,
            expected_hostname=expected_hostname,
            now=observed_at,
            max_age_seconds=max_clock_evidence_age_seconds,
        )
        offset_ms = _finite_number(clock.get("offset_ms"), field_name="clock offset_ms")
        clock_ok = (
            clock_age <= max_clock_evidence_age_seconds
            and clock.get("synchronized") is True
            and math.isfinite(offset_ms)
            and abs(offset_ms) <= max_clock_offset_ms
        )
        add("clock_synchronization", clock_ok, f"age={clock_age:.3f}s offset_ms={offset_ms}")
        evidence_hashes["clock"] = clock_sha
    except (RawLedgerPreflightError, TypeError, ValueError) as error:
        add(
            "clock_synchronization",
            False,
            type(error).__name__,
            unknown=not clock_evidence.exists(),
        )

    try:
        signed, attestation_age, attestation_hashes, signature_ok = _signed_attestation_evidence(
            payload_path=attestation,
            signature_path=attestation_signature,
            public_key_path=attestation_public_key,
            expected_hostname=expected_hostname,
            now=observed_at,
        )
        public_key_sha = attestation_hashes["public_key"]
        signature_sha = attestation_hashes["signature"]
        attestation_sha = attestation_hashes["attestation"]
        key_pinned = public_key_sha == expected_attestation_public_key_sha256
        bindings = signed.get("bindings")
        signed_metrics = signed.get("metrics")
        controls = signed.get("controls")
        capacity = signed.get("capacity")
        if not isinstance(bindings, dict):
            raise RawLedgerPreflightError("attestation bindings must be an object")
        if not isinstance(signed_metrics, dict):
            raise RawLedgerPreflightError("attestation metrics must be an object")
        if not isinstance(controls, dict):
            raise RawLedgerPreflightError("attestation controls must be an object")
        if not isinstance(capacity, dict):
            raise RawLedgerPreflightError("attestation capacity must be an object")

        expected_bindings: dict[str, Any] = {
            "policy_sha256": policy_sha256,
            "collection_root_sha256": collection_root_sha256,
            "collector_env_sha256": env_sha256,
            "backup_manifest_sha256": evidence_hashes["backup_manifest"],
            "alert_evidence_sha256": evidence_hashes["alert"],
            "clock_evidence_sha256": evidence_hashes["clock"],
            "required_symbols": list(symbols),
            "deployed_sha": deployed_sha,
        }
        bindings_ok = bindings == expected_bindings
        signed_attempts = int(signed_metrics.get("raw_attempted", -1))
        signed_metrics_at = _aware_stamp(
            signed_metrics.get("observed_at"), field_name="signed metrics observed_at"
        )
        current_metrics_at = (
            _aware_stamp(metrics_summary["observed_at"], field_name="current metrics observed_at")
            if metrics_summary is not None
            else signed_metrics_at - timedelta(seconds=1)
        )
        metrics_progress_seconds = (current_metrics_at - signed_metrics_at).total_seconds()
        current_attempts = (
            int(metrics_summary["raw_attempted"]) if metrics_summary is not None else -1
        )
        attempted_progress = current_attempts - signed_attempts
        attempt_rate_ok = metrics_progress_seconds >= 0 and 0 <= attempted_progress <= math.ceil(
            max_raw_attempt_rate_per_second * metrics_progress_seconds
        )
        signed_counter_snapshot = signed_metrics.get("counter_snapshot")
        current_counter_snapshot = (
            metrics_summary.get("counter_snapshot") if metrics_summary is not None else None
        )
        counters_monotonic = bool(
            isinstance(signed_counter_snapshot, dict)
            and isinstance(current_counter_snapshot, dict)
            and signed_counter_snapshot.keys() == current_counter_snapshot.keys()
            and int(signed_counter_snapshot.get("raw_attempted", -1)) == signed_attempts
            and int(signed_counter_snapshot.get("raw_failed", -1))
            == int(signed_metrics.get("raw_failed", -2))
            and int(signed_counter_snapshot.get("raw_queue_overflow", -1))
            == int(signed_metrics.get("raw_queue_overflow", -2))
            and all(
                int(signed_counter_snapshot[key]) <= int(current_counter_snapshot[key])
                for key in signed_counter_snapshot
            )
        )
        signed_trial_summary = (
            _trial_ledger_summary(
                live_evidence=live_evidence,
                symbols=symbols,
                started_at=_aware_stamp(
                    signed_metrics.get("run_started_at"),
                    field_name="signed metrics run_started_at",
                ),
                ended_at=signed_metrics_at,
            )
            if live_evidence is not None
            else None
        )
        signed_trial_counts = (
            signed_trial_summary.get("symbol_counts")
            if isinstance(signed_trial_summary, dict)
            else None
        )
        signed_trial_ok = bool(
            signed_metrics.get("trial_ledger") == signed_trial_summary
            and isinstance(signed_trial_counts, dict)
            and all(
                int(signed_trial_counts.get(symbol, 0)) >= min_live_events_per_symbol
                for symbol in symbols
            )
        )
        signed_metrics_file_sha = signed_metrics.get("metrics_file_sha256")
        metrics_bound = bool(
            metrics_summary is not None
            and signed_metrics.get("run_started_at") == metrics_summary["run_started_at"]
            and signed_metrics.get("writer_pid") == metrics_summary["writer_pid"]
            and signed_attempts >= min_raw_attempts
            and attempt_rate_ok
            and counters_monotonic
            and signed_trial_ok
            and isinstance(signed_metrics_file_sha, str)
            and len(signed_metrics_file_sha) == 64
            and all(character in "0123456789abcdef" for character in signed_metrics_file_sha)
            and signed_metrics.get("process_command_sha256")
            == metrics_summary["process_command_sha256"]
            and signed_metrics.get("process_executable_sha256")
            == metrics_summary["process_executable_sha256"]
            and signed_metrics.get("process_started_at") == metrics_summary["process_started_at"]
            and signed_metrics.get("lock_inode") == metrics_summary["lock_inode"]
            and signed_metrics.get("on_disk_code_sha256") == metrics_summary["on_disk_code_sha256"]
            and signed_metrics.get("collector_env_sha256")
            == metrics_summary["collector_env_sha256"]
            and signed_metrics.get("runtime_interpreter_sha256")
            == metrics_summary["runtime_interpreter_sha256"]
            and signed_metrics.get("runtime_environment_sha256")
            == metrics_summary["runtime_environment_sha256"]
            and signed_metrics.get("runtime_environment_lock_sha256")
            == metrics_summary["runtime_environment_lock_sha256"]
            and signed_metrics.get("runtime_isolated") is metrics_summary["runtime_isolated"]
        )
        required_controls = (
            "backup_independently_retained",
            "backup_physical_device_separate",
            "writer_unique",
            "deployed_sha_verified",
            "worktree_clean",
            "risk_config_reviewed",
            "daily_growth_cap_reviewed",
            "backup_capacity_reviewed",
            "python_executable_reviewed",
            "collector_environment_lock_reviewed",
            "full_snapshot_capacity_reviewed",
        )
        controls_ok = all(controls.get(key) is True for key in required_controls)
        observed_daily_growth = int(capacity.get("observed_daily_growth_bytes", -1))
        reviewed_collection_bytes = int(capacity.get("current_collection_bytes", -1))
        reviewed_backup_projection = (
            backup_retention_snapshots * reviewed_collection_bytes
            + max_daily_growth_bytes
            * backup_retention_snapshots
            * (backup_retention_snapshots + 1)
            // 2
        )
        capacity_ok = (
            0 < observed_daily_growth <= max_daily_growth_bytes
            and capacity.get("reviewed_max_daily_growth_bytes") == max_daily_growth_bytes
            and capacity.get("retention_days") == retention_days
            and capacity.get("backup_strategy") == "full_create_only"
            and capacity.get("backup_retention_snapshots") == backup_retention_snapshots
            and current_collection_bytes is not None
            and 0 < reviewed_collection_bytes <= current_collection_bytes
            and capacity.get("projected_backup_series_bytes") == reviewed_backup_projection
            and projected_backup_series_bytes is not None
            and reviewed_backup_projection <= projected_backup_series_bytes
        )
        attestation_ok = (
            0 <= attestation_age <= max_attestation_age_seconds
            and signature_ok
            and key_pinned
            and bindings_ok
            and metrics_bound
            and controls_ok
            and capacity_ok
        )
        add(
            "independent_signed_attestation",
            attestation_ok,
            (
                f"age={attestation_age:.3f}s signature_ok={signature_ok} "
                f"key_pinned={key_pinned} bindings_ok={bindings_ok} "
                f"metrics_bound={metrics_bound} controls_ok={controls_ok} "
                f"capacity_ok={capacity_ok}"
            ),
        )
        evidence_hashes["attestation"] = attestation_sha
        evidence_hashes["attestation_signature"] = signature_sha
        evidence_hashes["attestation_public_key"] = public_key_sha
    except (OSError, TypeError, ValueError, RawLedgerPreflightError) as error:
        add(
            "independent_signed_attestation",
            False,
            type(error).__name__,
            unknown=not attestation.exists(),
        )

    ready = bool(checks) and all(check.status == "pass" for check in checks)
    return {
        "contract": PREFLIGHT_CONTRACT,
        "observed_at": observed_at.isoformat(),
        "decision": "ready" if ready else "abstain",
        "ready": ready,
        "observed_hostname": observed_hostname,
        "expected_hostname": expected_hostname,
        "required_symbols": list(symbols),
        "policy": policy,
        "disk": {
            "free_bytes": disk.free,
            "projected_retention_bytes": projected_retention_bytes,
            "required_free_bytes": required_free_bytes,
            "backup_free_bytes": backup_disk.free if backup_disk is not None else None,
            "required_backup_free_bytes": required_backup_free_bytes,
            "current_collection_bytes": current_collection_bytes,
            "projected_backup_series_bytes": projected_backup_series_bytes,
        },
        "checks": [asdict(check) for check in checks],
        "evidence_sha256": evidence_hashes,
    }


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    if path.exists() or path.is_symlink():
        raise RawLedgerPreflightError(f"preflight output already exists: {path}")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except FileExistsError as error:
        raise RawLedgerPreflightError(f"preflight output already exists: {path}") from error
    finally:
        temporary.unlink(missing_ok=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--collection-root", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--expected-hostname", required=True)
    parser.add_argument("--expected-oanda-environment", choices=("live",), required=True)
    parser.add_argument("--required-symbol", action="append", required=True)
    parser.add_argument("--stale-after-seconds", type=float, required=True)
    parser.add_argument("--max-duplicate-ratio", type=float, required=True)
    parser.add_argument("--max-quarantine-ratio", type=float, required=True)
    parser.add_argument("--max-raw-failure-ratio", type=float, required=True)
    parser.add_argument("--max-raw-queue-overflow-ratio", type=float, required=True)
    parser.add_argument("--max-annotation-failure-ratio", type=float, required=True)
    parser.add_argument("--max-annotation-queue-overflow-ratio", type=float, required=True)
    parser.add_argument("--max-raw-attempt-rate-per-second", type=float, required=True)
    parser.add_argument("--max-queue-backlog", type=int, required=True)
    parser.add_argument("--min-shadow-observation-seconds", type=float, required=True)
    parser.add_argument("--min-raw-attempts", type=int, required=True)
    parser.add_argument("--min-live-events-per-symbol", type=int, required=True)
    parser.add_argument("--min-free-bytes", type=int, required=True)
    parser.add_argument("--min-backup-free-bytes", type=int, required=True)
    parser.add_argument("--retention-days", type=int, required=True)
    parser.add_argument("--backup-retention-snapshots", type=int, required=True)
    parser.add_argument("--max-daily-growth-bytes", type=int, required=True)
    parser.add_argument("--backup-root", type=Path, required=True)
    parser.add_argument("--max-backup-age-seconds", type=float, required=True)
    parser.add_argument("--max-backup-lag-events", type=int, required=True)
    parser.add_argument("--max-backup-lag-annotations", type=int, required=True)
    parser.add_argument("--alert-evidence", type=Path, required=True)
    parser.add_argument("--max-alert-evidence-age-seconds", type=float, required=True)
    parser.add_argument("--clock-evidence", type=Path, required=True)
    parser.add_argument("--max-clock-evidence-age-seconds", type=float, required=True)
    parser.add_argument("--max-clock-offset-ms", type=float, required=True)
    parser.add_argument("--attestation", type=Path, required=True)
    parser.add_argument("--attestation-signature", type=Path, required=True)
    parser.add_argument("--attestation-public-key", type=Path, required=True)
    parser.add_argument("--expected-attestation-public-key-sha256", required=True)
    parser.add_argument("--max-attestation-age-seconds", type=float, required=True)
    parser.add_argument("--expected-deployed-sha", required=True)
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    if not python_runtime_isolated():
        print(
            "raw-ledger preflight requires Python -I -S -B -X pycache_prefix=/dev/null",
            file=sys.stderr,
        )
        return 2
    args = build_parser().parse_args(argv)
    try:
        report = audit_preflight(
            collection_root=args.collection_root,
            env_file=args.env_file,
            expected_hostname=args.expected_hostname,
            expected_oanda_environment=args.expected_oanda_environment,
            required_symbols=tuple(args.required_symbol),
            stale_after_seconds=args.stale_after_seconds,
            max_duplicate_ratio=args.max_duplicate_ratio,
            max_quarantine_ratio=args.max_quarantine_ratio,
            max_raw_failure_ratio=args.max_raw_failure_ratio,
            max_raw_queue_overflow_ratio=args.max_raw_queue_overflow_ratio,
            max_annotation_failure_ratio=args.max_annotation_failure_ratio,
            max_annotation_queue_overflow_ratio=(args.max_annotation_queue_overflow_ratio),
            max_raw_attempt_rate_per_second=args.max_raw_attempt_rate_per_second,
            max_queue_backlog=args.max_queue_backlog,
            min_shadow_observation_seconds=args.min_shadow_observation_seconds,
            min_raw_attempts=args.min_raw_attempts,
            min_live_events_per_symbol=args.min_live_events_per_symbol,
            min_free_bytes=args.min_free_bytes,
            min_backup_free_bytes=args.min_backup_free_bytes,
            retention_days=args.retention_days,
            backup_retention_snapshots=args.backup_retention_snapshots,
            max_daily_growth_bytes=args.max_daily_growth_bytes,
            backup_root=args.backup_root,
            max_backup_age_seconds=args.max_backup_age_seconds,
            max_backup_lag_events=args.max_backup_lag_events,
            max_backup_lag_annotations=args.max_backup_lag_annotations,
            alert_evidence=args.alert_evidence,
            max_alert_evidence_age_seconds=args.max_alert_evidence_age_seconds,
            clock_evidence=args.clock_evidence,
            max_clock_evidence_age_seconds=args.max_clock_evidence_age_seconds,
            max_clock_offset_ms=args.max_clock_offset_ms,
            attestation=args.attestation,
            attestation_signature=args.attestation_signature,
            attestation_public_key=args.attestation_public_key,
            expected_attestation_public_key_sha256=(args.expected_attestation_public_key_sha256),
            max_attestation_age_seconds=args.max_attestation_age_seconds,
            expected_deployed_sha=args.expected_deployed_sha,
        )
        if args.output is not None:
            _atomic_write_json(args.output, report)
    except (OSError, RawLedgerPreflightError) as error:
        print(
            json.dumps(
                {"decision": "invalid", "error_type": type(error).__name__, "detail": str(error)},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["ready"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
