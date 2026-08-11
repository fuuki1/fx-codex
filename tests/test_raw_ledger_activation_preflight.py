"""Fail-closed readiness evidence for raw-ledger shadow activation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from data_platform.contracts.raw_event import RawEvent
from data_platform.raw.backup import create_backup, verify_backup
from data_platform.raw.event_ledger import RawEventLedger
from data_platform.raw.immutable_store import ImmutableRawStore
from tools import raw_ledger_activation_preflight as preflight
from tools.run_exclusive import ExclusiveLock

NOW = datetime.now(UTC).replace(microsecond=0)
EXPECTED_HOST = "fx-mini.test"
EXPECTED_SHA = "a" * 40
SYMBOLS = ("USD_JPY", "EUR_USD", "GBP_USD")


def _counter_snapshot() -> dict[str, int]:
    return {
        "attempted": 3,
        "queued": 3,
        "stored": 3,
        "idempotent": 0,
        "failed": 0,
        "queue_overflow": 0,
        "raw_attempted": 3,
        "raw_queued": 3,
        "raw_stored": 3,
        "raw_idempotent": 0,
        "raw_failed": 0,
        "raw_queue_overflow": 0,
        "annotations_attempted": 0,
        "annotations_queued": 0,
        "annotations_stored": 0,
        "annotations_idempotent": 0,
        "annotations_failed": 0,
        "annotations_queue_overflow": 0,
        "failure_reports_queued": 0,
    }


def _event(
    index: int,
    symbol: str,
    *,
    live_source: bool = True,
    source_time_delta: timedelta = timedelta(0),
) -> RawEvent:
    received = NOW - timedelta(seconds=5 - index)
    source_time = received + source_time_delta
    payload = json.dumps(
        {
            "type": "PRICE",
            "instrument": f"{symbol[:3]}_{symbol[3:]}",
            "time": source_time.isoformat(),
            "bids": [{"price": "150.001", "liquidity": 1_000_000}],
            "asks": [{"price": "150.004", "liquidity": 1_000_000}],
            "tradeable": True,
        }
    ).encode()
    payload_hash = hashlib.sha256(payload).hexdigest()
    connection_id = f"connection-{index}"
    source_event_id = hashlib.sha256(
        f"price|{symbol}|{source_time.isoformat()}".encode()
    ).hexdigest()
    event_id = hashlib.sha256(
        "|".join(
            (
                "oanda-pricing-stream",
                connection_id,
                received.isoformat(),
                payload_hash,
            )
        ).encode()
    ).hexdigest()
    account_hash = hashlib.sha256(b"secret-account").hexdigest()
    return RawEvent(
        event_id=event_id if live_source else f"fixture-{index}",
        source="oanda-pricing-stream" if live_source else "fixture",
        source_event_id=source_event_id,
        event_type="price",
        symbol=symbol,
        event_time=source_time,
        receive_time=received,
        ingest_time=received + timedelta(milliseconds=10),
        payload_bytes=payload,
        payload_hash=payload_hash,
        provenance={
            "writer": "fx_quote_collector" if live_source else "test",
            "source_event_id_scope": (
                f"pricing_stream:live:account:{account_hash}:" "event_type:instrument:event_time:v1"
            ),
            "account_id_sha256": account_hash,
            "account_environment": "live" if live_source else "fixture",
            "collection_mode": "live_stream" if live_source else "fixture",
            "connection_id": connection_id,
            "source_endpoint_class": ("streaming_pricing" if live_source else "test_fixture"),
        },
    )


def _policy() -> dict[str, object]:
    return {
        "stale_after_seconds": 60,
        "max_duplicate_ratio": 0.1,
        "max_quarantine_ratio": 0.1,
        "max_raw_failure_ratio": 0,
        "max_raw_queue_overflow_ratio": 0,
        "max_annotation_failure_ratio": 0,
        "max_annotation_queue_overflow_ratio": 0,
        "max_raw_attempt_rate_per_second": 100,
        "max_queue_backlog": 0,
        "min_shadow_observation_seconds": 60,
        "min_raw_attempts": 3,
        "min_live_events_per_symbol": 1,
        "min_free_bytes": 1_000,
        "min_backup_free_bytes": 1_000,
        "retention_days": 30,
        "backup_retention_snapshots": 30,
        "max_daily_growth_bytes": 100,
        "max_backup_age_seconds": 60,
        "max_backup_lag_events": 0,
        "max_backup_lag_annotations": 0,
        "max_alert_evidence_age_seconds": 60,
        "max_clock_evidence_age_seconds": 60,
        "max_clock_offset_ms": 100,
        "max_attestation_age_seconds": 60,
        "expected_deployed_sha": EXPECTED_SHA,
    }


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sign(payload: Path, private_key: Path, public_key: Path, signature: Path) -> None:
    subprocess.run(
        [str(preflight.OPENSSL_PATH), "genrsa", "-out", str(private_key), "2048"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            str(preflight.OPENSSL_PATH),
            "rsa",
            "-in",
            str(private_key),
            "-pubout",
            "-out",
            str(public_key),
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            str(preflight.OPENSSL_PATH),
            "dgst",
            "-sha256",
            "-sign",
            str(private_key),
            "-out",
            str(signature),
            str(payload),
        ],
        check=True,
        capture_output=True,
    )


def _fixture(tmp_path: Path, *, live_source: bool = True) -> dict[str, Path]:
    root = tmp_path / "collect"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    ledger = RawEventLedger(
        root / "raw-ledger",
        blob_store=ImmutableRawStore(root / "raw"),
    )
    for index, symbol in enumerate(("USDJPY", "EURUSD", "GBPUSD"), start=1):
        ledger.append(_event(index, symbol, live_source=live_source))

    state = root / "state"
    state.mkdir()
    metrics = state / "raw_ledger_shadow_metrics.json"
    metrics.write_text(
        json.dumps(
            {
                "observed_at": NOW.isoformat(),
                "run_started_at": (NOW - timedelta(seconds=120)).isoformat(),
                "enabled": True,
                "final": False,
                "worker_alive": True,
                "failure_worker_alive": False,
                "writer_pid": os.getpid(),
                **_counter_snapshot(),
                "raw_failure_ratio": 0.0,
                "raw_queue_overflow_ratio": 0.0,
                "annotation_failure_ratio": 0.0,
                "annotation_queue_overflow_ratio": 0.0,
                "queue_overflow_ratio": 0.0,
                "queue_backlog": 0,
                "failure_queue_backlog": 0,
                "unfinished_tasks": 0,
                "failure_unfinished_tasks": 0,
                "failure_report_queue_overflow": 0,
                "failure_report_errors": 0,
                "failure_log_write_errors": 0,
                "reporter_write_failures": 0,
                "collector_env_sha256": hashlib.sha256(
                    b"FX_OANDA_API_TOKEN=secret-token\n"
                    b"FX_OANDA_ACCOUNT_ID=secret-account\n"
                    b"FX_OANDA_ENV=live\n"
                    b"FX_RAW_LEDGER_ENABLED=true\n"
                ).hexdigest(),
                "runtime_on_disk_code_sha256": "d" * 64,
                "runtime_interpreter_sha256": "e" * 64,
                "runtime_environment_sha256": "f" * 64,
                "runtime_environment_lock_sha256": "1" * 64,
                "runtime_isolated": True,
            }
        ),
        encoding="utf-8",
    )
    env_file = tmp_path / "collector.env"
    env_file.write_text(
        "FX_OANDA_API_TOKEN=secret-token\n"
        "FX_OANDA_ACCOUNT_ID=secret-account\n"
        "FX_OANDA_ENV=live\n"
        "FX_RAW_LEDGER_ENABLED=true\n",
        encoding="utf-8",
    )
    env_file.chmod(0o600)

    backup = tmp_path / "backups" / "latest"
    create_backup(collection_root=root, destination=backup, now=NOW)
    backup_result = verify_backup(backup, now=NOW)
    alert = tmp_path / "alert.json"
    alert.write_text(
        json.dumps(
            {
                "contract": preflight.ALERT_EVIDENCE_CONTRACT,
                "hostname": EXPECTED_HOST,
                "observed_at": NOW.isoformat(),
                "route_configured": True,
                "delivery_test_passed": True,
                "raw_ledger_health_routed": True,
            }
        ),
        encoding="utf-8",
    )
    clock = tmp_path / "clock.json"
    clock.write_text(
        json.dumps(
            {
                "contract": preflight.CLOCK_EVIDENCE_CONTRACT,
                "hostname": EXPECTED_HOST,
                "observed_at": NOW.isoformat(),
                "synchronized": True,
                "offset_ms": 2.5,
            }
        ),
        encoding="utf-8",
    )

    attestation = tmp_path / "attestation.json"
    attestation.write_text(
        json.dumps(
            {
                "contract": preflight.ATTESTATION_CONTRACT,
                "hostname": EXPECTED_HOST,
                "observed_at": NOW.isoformat(),
                "bindings": {
                    "policy_sha256": preflight._json_sha256(_policy()),
                    "collection_root_sha256": hashlib.sha256(
                        str(root.absolute()).encode()
                    ).hexdigest(),
                    "collector_env_sha256": _sha256(env_file),
                    "backup_manifest_sha256": backup_result["manifest_sha256"],
                    "alert_evidence_sha256": _sha256(alert),
                    "clock_evidence_sha256": _sha256(clock),
                    "required_symbols": ["EURUSD", "GBPUSD", "USDJPY"],
                    "deployed_sha": EXPECTED_SHA,
                },
                "metrics": {
                    "run_started_at": (NOW - timedelta(seconds=120)).isoformat(),
                    "observed_at": NOW.isoformat(),
                    "raw_attempted": 3,
                    "raw_failed": 0,
                    "raw_queue_overflow": 0,
                    "writer_pid": os.getpid(),
                    "metrics_file_sha256": _sha256(metrics),
                    "counter_snapshot": _counter_snapshot(),
                    "trial_ledger": {
                        "symbol_counts": {"EURUSD": 1, "GBPUSD": 1, "USDJPY": 1},
                        "event_ids_sha256": hashlib.sha256(
                            (
                                "\n".join(
                                    sorted(
                                        _event(index, symbol).event_id
                                        for index, symbol in enumerate(
                                            ("USDJPY", "EURUSD", "GBPUSD"), start=1
                                        )
                                    )
                                )
                                + "\n"
                            ).encode()
                        ).hexdigest(),
                    },
                    "process_command_sha256": "b" * 64,
                    "process_executable_sha256": "c" * 64,
                    "process_started_at": (NOW - timedelta(seconds=121)).isoformat(),
                    "lock_inode": 123,
                    "on_disk_code_sha256": "d" * 64,
                    "collector_env_sha256": _sha256(env_file),
                    "runtime_interpreter_sha256": "e" * 64,
                    "runtime_environment_sha256": "f" * 64,
                    "runtime_environment_lock_sha256": "1" * 64,
                    "runtime_isolated": True,
                },
                "controls": {
                    "backup_independently_retained": True,
                    "backup_physical_device_separate": True,
                    "writer_unique": True,
                    "deployed_sha_verified": True,
                    "worktree_clean": True,
                    "risk_config_reviewed": True,
                    "daily_growth_cap_reviewed": True,
                    "backup_capacity_reviewed": True,
                    "python_executable_reviewed": True,
                    "collector_environment_lock_reviewed": True,
                    "full_snapshot_capacity_reviewed": True,
                },
                "capacity": {
                    "observed_daily_growth_bytes": 50,
                    "reviewed_max_daily_growth_bytes": 100,
                    "retention_days": 30,
                    "backup_strategy": "full_create_only",
                    "backup_retention_snapshots": 30,
                    "current_collection_bytes": preflight._directory_size(root),
                    "projected_backup_series_bytes": (
                        30 * preflight._directory_size(root) + 100 * 30 * 31 // 2
                    ),
                },
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    private_key = tmp_path / "reviewer-private.pem"
    public_key = tmp_path / "reviewer-public.pem"
    signature = tmp_path / "attestation.sig"
    _sign(attestation, private_key, public_key, signature)
    return {
        "root": root,
        "env": env_file,
        "metrics": metrics,
        "backup": backup,
        "alert": alert,
        "clock": clock,
        "attestation": attestation,
        "signature": signature,
        "public_key": public_key,
    }


def _audit(paths: dict[str, Path], *, environment: str = "live") -> dict[str, object]:
    return preflight.audit_preflight(
        collection_root=paths["root"],
        env_file=paths["env"],
        expected_hostname=EXPECTED_HOST,
        expected_oanda_environment=environment,
        required_symbols=SYMBOLS,
        stale_after_seconds=60,
        max_duplicate_ratio=0.1,
        max_quarantine_ratio=0.1,
        max_raw_failure_ratio=0,
        max_raw_queue_overflow_ratio=0,
        max_annotation_failure_ratio=0,
        max_annotation_queue_overflow_ratio=0,
        max_raw_attempt_rate_per_second=100,
        max_queue_backlog=0,
        min_shadow_observation_seconds=60,
        min_raw_attempts=3,
        min_live_events_per_symbol=1,
        min_free_bytes=1_000,
        min_backup_free_bytes=1_000,
        retention_days=30,
        backup_retention_snapshots=30,
        max_daily_growth_bytes=100,
        backup_root=paths["backup"],
        max_backup_age_seconds=60,
        max_backup_lag_events=0,
        max_backup_lag_annotations=0,
        alert_evidence=paths["alert"],
        max_alert_evidence_age_seconds=60,
        clock_evidence=paths["clock"],
        max_clock_evidence_age_seconds=60,
        max_clock_offset_ms=100,
        attestation=paths["attestation"],
        attestation_signature=paths["signature"],
        attestation_public_key=paths["public_key"],
        expected_attestation_public_key_sha256=_sha256(paths["public_key"]),
        max_attestation_age_seconds=60,
        expected_deployed_sha=EXPECTED_SHA,
        now=NOW,
    )


def _patch_ready_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(preflight.socket, "gethostname", lambda: EXPECTED_HOST)
    monkeypatch.setattr(preflight, "_git_state", lambda: (EXPECTED_SHA, True))
    monkeypatch.setattr(
        preflight,
        "_writer_process_identity",
        lambda **_kwargs: {
            "ok": True,
            "command_sha256": "b" * 64,
            "executable_sha256": "c" * 64,
            "process_started_at": (NOW - timedelta(seconds=121)).isoformat(),
            "lock_inode": 123,
            "on_disk_code_sha256": "d" * 64,
            "approved_interpreter_sha256": "e" * 64,
            "approved_environment_sha256": "f" * 64,
            "approved_environment_lock_sha256": "1" * 64,
            "approved_venv": True,
            "isolated_options_ok": True,
            "latest_code_mtime": 0,
            "code_deployed_before_process_start": True,
            "start_delta_seconds": 1,
        },
    )
    monkeypatch.setattr(
        preflight.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(total=1_000_000_000, used=1_000, free=999_999_000),
    )


def test_writer_identity_path_options_reject_argparse_last_value_override(
    tmp_path: Path,
) -> None:
    expected = tmp_path / "expected.env"
    other = tmp_path / "other.env"
    assert preflight._single_path_option_matches(
        ["collector.py", "--env-file", str(expected)],
        "--env-file",
        expected,
    )
    assert not preflight._single_path_option_matches(
        [
            "collector.py",
            "--env-file",
            str(expected),
            "--env-file",
            str(other),
        ],
        "--env-file",
        expected,
    )


def test_preflight_passes_only_with_pinned_signed_bound_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_ready_host(monkeypatch)
    paths = _fixture(tmp_path)
    monkeypatch.setattr(
        preflight,
        "_device_id",
        lambda path: 2 if path == paths["backup"] else 1,
    )
    report = _audit(paths)

    assert report["ready"] is True
    assert report["decision"] == "ready"
    assert all(check["status"] == "pass" for check in report["checks"])
    encoded = json.dumps(report)
    assert "secret-token" not in encoded
    assert "secret-account" not in encoded


def test_preflight_recomputes_capacity_after_signed_checkpoint_growth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_ready_host(monkeypatch)
    paths = _fixture(tmp_path)
    signed_collection_bytes = preflight._directory_size(paths["root"])
    (paths["root"] / "state" / "collector.log").write_bytes(b"post-review-growth")
    current_collection_bytes = preflight._directory_size(paths["root"])
    assert current_collection_bytes > signed_collection_bytes
    monkeypatch.setattr(
        preflight,
        "_device_id",
        lambda path: 2 if path == paths["backup"] else 1,
    )

    report = _audit(paths)
    checks = {check["key"]: check for check in report["checks"]}

    assert report["ready"] is True
    assert checks["backup_retention_capacity"]["status"] == "pass"
    assert checks["independent_signed_attestation"]["status"] == "pass"
    assert report["disk"]["current_collection_bytes"] == current_collection_bytes


def test_preflight_abstains_on_same_device_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_ready_host(monkeypatch)
    paths = _fixture(tmp_path)
    report = _audit(paths)
    checks = {check["key"]: check for check in report["checks"]}

    assert report["decision"] == "abstain"
    assert checks["backup_restore"]["status"] == "fail"
    assert "separate_device=False" in checks["backup_restore"]["detail"]


def test_preflight_rejects_non_live_activation_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_ready_host(monkeypatch)
    paths = _fixture(tmp_path)

    with pytest.raises(preflight.RawLedgerPreflightError, match="requires OANDA environment live"):
        _audit(paths, environment="practice")


def test_preflight_rejects_fixture_events_as_live_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_ready_host(monkeypatch)
    paths = _fixture(tmp_path, live_source=False)
    monkeypatch.setattr(preflight, "_device_id", lambda _path: 1)
    report = _audit(paths)
    checks = {check["key"]: check for check in report["checks"]}

    assert checks["oanda_live_stream_coverage"]["status"] == "fail"


@pytest.mark.parametrize(
    "source_time_delta",
    (timedelta(days=7), timedelta(days=-7)),
)
def test_live_coverage_rejects_future_or_replayed_source_time(
    tmp_path: Path, source_time_delta: timedelta
) -> None:
    root = tmp_path / "collect"
    root.mkdir(mode=0o700)
    ledger = RawEventLedger(root / "raw-ledger", blob_store=ImmutableRawStore(root / "raw"))
    for index, symbol in enumerate(("USDJPY", "EURUSD", "GBPUSD"), start=1):
        ledger.append(_event(index, symbol, source_time_delta=source_time_delta))

    evidence = preflight._live_ledger_evidence(
        collection_root=root,
        required_symbols=("EURUSD", "GBPUSD", "USDJPY"),
        now=NOW,
        stale_after_seconds=60,
        min_events_per_symbol=1,
        expected_account_id_sha256=hashlib.sha256(b"secret-account").hexdigest(),
        max_source_time_lead_seconds=0.1,
    )

    assert evidence["coverage_ok"] is False
    assert evidence["counts"] == {"EURUSD": 0, "GBPUSD": 0, "USDJPY": 0}


def test_writer_lock_probe_verifies_real_flock_owner(tmp_path: Path) -> None:
    locks = tmp_path / "state"
    lock = ExclusiveLock("quote-collector", locks_dir=locks)
    assert lock.acquire() is True
    try:
        proven, inode = preflight._probe_writer_lock(
            lock_path=lock.path,
            pid=os.getpid(),
            hostname=preflight.socket.gethostname(),
        )
    finally:
        lock.release()

    assert proven is True
    assert inode == lock.path.stat().st_ino


def test_preflight_rejects_annotation_added_after_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_ready_host(monkeypatch)
    paths = _fixture(tmp_path)
    ledger = RawEventLedger(
        paths["root"] / "raw-ledger",
        blob_store=ImmutableRawStore(paths["root"] / "raw"),
    )
    ledger.annotate_quarantine(
        _event(1, "USDJPY").event_id,
        annotation_id="post-backup-quality-1",
        reason="test_quality_veto",
        annotated_at=NOW,
        provenance={"validator": "test"},
    )
    monkeypatch.setattr(
        preflight,
        "_device_id",
        lambda path: 2 if path == paths["backup"] else 1,
    )
    report = _audit(paths)
    checks = {check["key"]: check for check in report["checks"]}

    assert checks["backup_restore"]["status"] == "fail"
    assert "annotation_lag=1" in checks["backup_restore"]["detail"]


def test_preflight_rejects_tampered_attestation_signature(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_ready_host(monkeypatch)
    paths = _fixture(tmp_path)
    paths["attestation"].write_text("{}", encoding="utf-8")
    report = _audit(paths)
    checks = {check["key"]: check for check in report["checks"]}

    assert checks["independent_signed_attestation"]["status"] == "fail"


def test_preflight_recomputes_ratios_from_counters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_ready_host(monkeypatch)
    paths = _fixture(tmp_path)
    metrics = json.loads(paths["metrics"].read_text(encoding="utf-8"))
    metrics["raw_failure_ratio"] = 0.5
    paths["metrics"].write_text(json.dumps(metrics), encoding="utf-8")
    report = _audit(paths)
    checks = {check["key"]: check for check in report["checks"]}

    assert checks["shadow_runtime_metrics"]["status"] == "fail"
    assert "ratios_match_counters=False" in checks["shadow_runtime_metrics"]["detail"]


def test_preflight_rejects_missing_raw_storage_arithmetic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_ready_host(monkeypatch)
    paths = _fixture(tmp_path)
    metrics = json.loads(paths["metrics"].read_text(encoding="utf-8"))
    metrics["raw_stored"] = 0
    metrics["stored"] = 0
    paths["metrics"].write_text(json.dumps(metrics), encoding="utf-8")
    report = _audit(paths)
    checks = {check["key"]: check for check in report["checks"]}

    assert checks["shadow_runtime_metrics"]["status"] == "fail"
    assert "counter_arithmetic_ok=False" in checks["shadow_runtime_metrics"]["detail"]


def test_preflight_rejects_annotation_overflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_ready_host(monkeypatch)
    paths = _fixture(tmp_path)
    metrics = json.loads(paths["metrics"].read_text(encoding="utf-8"))
    metrics.update(
        {
            "attempted": 4,
            "failed": 1,
            "queue_overflow": 1,
            "annotations_attempted": 1,
            "annotations_failed": 1,
            "annotations_queue_overflow": 1,
            "annotation_failure_ratio": 1.0,
            "annotation_queue_overflow_ratio": 1.0,
            "queue_overflow_ratio": 0.25,
        }
    )
    paths["metrics"].write_text(json.dumps(metrics), encoding="utf-8")
    report = _audit(paths)
    checks = {check["key"]: check for check in report["checks"]}

    assert checks["shadow_runtime_metrics"]["status"] == "fail"
    assert "annotation_overflow_ratio=1.0" in checks["shadow_runtime_metrics"]["detail"]


def test_preflight_rejects_unsigned_metrics_counter_inflation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_ready_host(monkeypatch)
    paths = _fixture(tmp_path)
    metrics = json.loads(paths["metrics"].read_text(encoding="utf-8"))
    for key in ("attempted", "queued", "stored", "raw_attempted", "raw_queued", "raw_stored"):
        metrics[key] = 999_999
    paths["metrics"].write_text(json.dumps(metrics), encoding="utf-8")
    report = _audit(paths)
    checks = {check["key"]: check for check in report["checks"]}

    assert checks["shadow_runtime_metrics"]["status"] == "pass"
    assert checks["independent_signed_attestation"]["status"] == "fail"


def test_preflight_rejects_unbound_writer_process_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_ready_host(monkeypatch)
    paths = _fixture(tmp_path)
    monkeypatch.setattr(
        preflight,
        "_writer_process_identity",
        lambda **_kwargs: {
            "ok": False,
            "command_sha256": "b" * 64,
            "executable_sha256": "c" * 64,
            "process_started_at": (NOW - timedelta(seconds=121)).isoformat(),
            "lock_inode": 123,
            "on_disk_code_sha256": "d" * 64,
            "approved_interpreter_sha256": "e" * 64,
            "approved_environment_sha256": "f" * 64,
            "approved_environment_lock_sha256": "1" * 64,
            "approved_venv": True,
            "isolated_options_ok": True,
            "latest_code_mtime": 0,
            "code_deployed_before_process_start": True,
            "start_delta_seconds": 1,
        },
    )
    report = _audit(paths)
    checks = {check["key"]: check for check in report["checks"]}

    assert checks["shadow_runtime_metrics"]["status"] == "fail"
    assert "writer_identity=False" in checks["shadow_runtime_metrics"]["detail"]


def test_preflight_marks_missing_host_evidence_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_ready_host(monkeypatch)
    paths = _fixture(tmp_path)
    paths["alert"] = tmp_path / "missing-alert.json"
    monkeypatch.setattr(preflight.socket, "gethostname", lambda: "wrong-host")
    report = _audit(paths)
    checks = {check["key"]: check for check in report["checks"]}

    assert report["ready"] is False
    assert checks["expected_host"]["status"] == "fail"
    assert checks["freshness_alert_routing"]["status"] == "unknown"


def test_preflight_fails_retention_projection_without_headroom(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_ready_host(monkeypatch)
    paths = _fixture(tmp_path)
    monkeypatch.setattr(
        preflight.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(total=2_000, used=1_500, free=500),
    )
    report = _audit(paths)
    checks = {check["key"]: check for check in report["checks"]}

    assert report["ready"] is False
    assert checks["retention_capacity"]["status"] == "fail"
    assert checks["backup_retention_capacity"]["status"] == "fail"


def test_preflight_output_is_create_only(tmp_path: Path) -> None:
    output = tmp_path / "audit.json"
    preflight._atomic_write_json(output, {"ready": False})
    original = output.read_bytes()

    with pytest.raises(preflight.RawLedgerPreflightError, match="already exists"):
        preflight._atomic_write_json(output, {"ready": True})

    assert output.read_bytes() == original


def test_preflight_cli_requires_isolated_no_site_runtime() -> None:
    script = Path(preflight.__file__).resolve()
    rejected = subprocess.run(
        [sys.executable, str(script), "--help"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    isolated = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-B",
            "-X",
            "pycache_prefix=/dev/null",
            str(script),
            "--help",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert rejected.returncode == 2
    assert "requires Python -I -S -B" in rejected.stderr
    assert isolated.returncode == 0, isolated.stderr
    assert "--collection-root" in isolated.stdout
