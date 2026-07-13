from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import pytest

from fx_backtester.artifacts import (
    _event_provenance_status,
    _single_dataset_provenance_status,
    _validate_equity_curve,
    audit_run_artifacts,
)
from fx_backtester.cli import main


def _write_test_run(tmp_path: Path) -> tuple[Path, Path]:
    input_path = tmp_path / "prices.csv"
    input_path.write_bytes(Path("examples/sample_prices.csv").read_bytes())
    output_dir = tmp_path / "run"
    exit_code = main(
        [
            "backtest",
            "--data",
            str(input_path),
            "--strategy",
            "ma_cross",
            "--output-dir",
            str(output_dir),
            "--expected-frequency",
            "h",
        ]
    )
    assert exit_code == 0
    return output_dir, input_path


def _refresh_output_fingerprint(output_dir: Path, name: str) -> None:
    manifest_path = output_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    path = (
        output_dir
        / {
            "trade_log": "trade_log.csv",
            "data_qa": "data_qa.csv",
            "metrics": "metrics.json",
            "equity_curve": "equity_curve.csv",
        }[name]
    )
    raw = path.read_bytes()
    manifest["output_fingerprints"][name] = {
        "path": str(path.resolve()),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw),
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")


def test_artifact_audit_recomputes_output_and_input_fingerprints(tmp_path: Path) -> None:
    output_dir, input_path = _write_test_run(tmp_path)
    assert audit_run_artifacts(output_dir)["passed"] is True

    metrics_path = output_dir / "metrics.json"
    original_metrics = metrics_path.read_bytes()
    metrics_path.write_bytes(original_metrics + b"\n")
    output_tamper = audit_run_artifacts(output_dir)
    assert output_tamper["passed"] is False
    assert "output_fingerprints.metrics SHA-256 mismatch" in output_tamper["errors"]

    metrics_path.write_bytes(original_metrics)
    assert audit_run_artifacts(output_dir)["passed"] is True

    manifest_path = output_dir / "manifest.json"
    original_manifest = manifest_path.read_bytes()
    manifest = json.loads(original_manifest)
    manifest["output_fingerprints"]["metrics"]["path"] = str(input_path)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    path_tamper = audit_run_artifacts(output_dir)
    assert path_tamper["passed"] is False
    assert (
        "output_fingerprints.metrics.path does not point to the audited run file"
        in path_tamper["errors"]
    )

    manifest_path.write_bytes(original_manifest)
    assert audit_run_artifacts(output_dir)["passed"] is True

    input_path.write_bytes(input_path.read_bytes() + b"\n")
    input_tamper = audit_run_artifacts(output_dir)
    assert input_tamper["passed"] is False
    assert "inputs.data[0] SHA-256 mismatch" in input_tamper["errors"]


def test_unprovenanced_price_csv_can_pass_integrity_but_never_promotion(tmp_path: Path) -> None:
    output_dir, _ = _write_test_run(tmp_path)
    manifest_path = output_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert manifest["quality_gates"]["dataset_provenance"]["promotion_eligible"] is False
    assert manifest["quality_gates"]["promotion_eligible"] is False
    report = audit_run_artifacts(output_dir)
    assert report["passed"] is True
    assert report["promotion_eligible"] is False

    manifest["quality_gates"]["dataset_provenance"]["promotion_eligible"] = True
    manifest["quality_gates"]["promotion_eligible"] = True
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    tampered = audit_run_artifacts(output_dir)
    assert tampered["passed"] is False
    assert tampered["promotion_eligible"] is False


def test_provenanced_price_sidecar_is_recomputed_during_audit(tmp_path: Path) -> None:
    input_path = tmp_path / "licensed_prices.csv"
    prices = pd.read_csv("examples/sample_prices.csv")
    timestamps = pd.to_datetime(prices["timestamp"], utc=True).map(lambda value: value.isoformat())
    prices["timestamp"] = timestamps
    prices["source_time"] = timestamps
    prices["ingested_time"] = timestamps
    prices["available_time"] = timestamps
    prices.to_csv(input_path, index=False)
    data_hash = hashlib.sha256(input_path.read_bytes()).hexdigest()
    sidecar = Path(str(input_path.resolve()) + ".provenance.json")
    sidecar.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "dataset_id": "licensed-test-bars-v1",
                "synthetic": False,
                "promotion_use_approved": True,
                "data_sha256": data_hash,
                "source": {"name": "Test licensed feed", "uri": "https://example.test/feed"},
                "license": {
                    "name": "Test research license",
                    "uri": "https://example.test/license",
                    "allows_model_research": True,
                },
                "acquired_at": "2024-02-08T00:00:00Z",
                "point_in_time": {
                    "event_time_column": "timestamp",
                    "source_time_column": "source_time",
                    "ingested_time_column": "ingested_time",
                    "available_time_column": "available_time",
                    "timezone": "UTC",
                    "timestamp_semantics": "bar_close",
                    "revision_policy": "immutable",
                    "index_is_available_time": True,
                    "future_data_rejected": True,
                },
                "transformation_lineage": [
                    {
                        "step_id": "normalize-test-bars",
                        "code_version": "test-v1",
                        "input_sha256": hashlib.sha256(
                            Path("examples/sample_prices.csv").read_bytes()
                        ).hexdigest(),
                        "output_sha256": data_hash,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    output_dir = tmp_path / "run"

    assert (
        main(
            [
                "backtest",
                "--data",
                str(input_path),
                "--strategy",
                "ma_cross",
                "--output-dir",
                str(output_dir),
                "--expected-frequency",
                "h",
            ]
        )
        == 0
    )
    report = audit_run_artifacts(output_dir)
    assert report["passed"] is True
    assert report["promotion_eligible"] is False
    manifest = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))
    assert (
        "event input was not provided"
        in manifest["quality_gates"]["event_provenance"]["limitations"][0]
    )

    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    payload["promotion_use_approved"] = False
    sidecar.write_text(json.dumps(payload), encoding="utf-8")
    tampered = audit_run_artifacts(output_dir)
    assert tampered["passed"] is False
    assert tampered["promotion_eligible"] is False


def test_provenance_sidecar_cannot_alias_all_point_in_time_roles(tmp_path: Path) -> None:
    input_path = tmp_path / "aliased_clocks.csv"
    prices = pd.read_csv("examples/sample_prices.csv")
    prices["timestamp"] = pd.to_datetime(prices["timestamp"], utc=True).map(
        lambda value: value.isoformat()
    )
    prices.to_csv(input_path, index=False)
    data_hash = hashlib.sha256(input_path.read_bytes()).hexdigest()
    sidecar = Path(str(input_path.resolve()) + ".provenance.json")
    sidecar.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "dataset_id": "aliased-clocks",
                "synthetic": False,
                "promotion_use_approved": True,
                "data_sha256": data_hash,
                "source": {"name": "Test feed", "uri": "https://example.test/feed"},
                "license": {
                    "name": "Test license",
                    "uri": "https://example.test/license",
                    "allows_model_research": True,
                },
                "acquired_at": "2026-07-13T00:00:00Z",
                "point_in_time": {
                    "event_time_column": "timestamp",
                    "source_time_column": "timestamp",
                    "ingested_time_column": "timestamp",
                    "available_time_column": "timestamp",
                    "timezone": "UTC",
                    "timestamp_semantics": "bar_close",
                    "revision_policy": "immutable",
                    "index_is_available_time": True,
                    "future_data_rejected": True,
                },
                "transformation_lineage": [
                    {
                        "step_id": "self-asserted",
                        "code_version": "test-v1",
                        "input_sha256": data_hash,
                        "output_sha256": data_hash,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    status = _single_dataset_provenance_status(input_path)

    assert status["contract_valid"] is False
    assert status["promotion_eligible"] is False
    assert any("distinct columns" in reason for reason in status["limitations"])


def test_artifact_audit_fails_closed_on_invalid_manifest_json(tmp_path: Path) -> None:
    output_dir, _ = _write_test_run(tmp_path)
    (output_dir / "manifest.json").write_text("{", encoding="utf-8")

    report = audit_run_artifacts(output_dir)

    assert report["passed"] is False
    assert any(error.startswith("manifest could not be read:") for error in report["errors"])


def test_run_artifacts_refuse_to_overwrite_existing_run(tmp_path: Path) -> None:
    _write_test_run(tmp_path)

    with pytest.raises(FileExistsError, match="immutable"):
        _write_test_run(tmp_path)


def test_artifact_audit_never_promotes_heuristic_event_identity(tmp_path: Path) -> None:
    input_path = tmp_path / "prices.csv"
    input_path.write_bytes(Path("examples/sample_prices.csv").read_bytes())
    events_path = tmp_path / "event_history.csv"
    events_path.write_text(
        "timestamp,currency,impact,name,occurrence_id,revision,effective_from,effective_to,"
        "is_tombstone,identity_quality,recorded_at\n"
        "2024-01-02T00:00:00Z,USD,high,NFP,heuristic:nfp,1,2024-01-01T00:00:00Z,,"
        "false,heuristic,2024-01-01T00:00:00Z\n",
        encoding="utf-8",
    )
    output_dir = tmp_path / "run"

    exit_code = main(
        [
            "backtest",
            "--data",
            str(input_path),
            "--events",
            str(events_path),
            "--strategy",
            "ma_cross",
            "--output-dir",
            str(output_dir),
            "--expected-frequency",
            "h",
        ]
    )

    assert exit_code == 0
    manifest_path = output_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["quality_gates"]["promotion_eligible"] is False
    assert manifest["quality_gates"]["event_provenance"]["stable_occurrence_identity"] is False
    report = audit_run_artifacts(output_dir)
    assert report["passed"] is True
    assert report["promotion_eligible"] is False

    manifest["quality_gates"]["promotion_eligible"] = True
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    tampered = audit_run_artifacts(output_dir)
    assert tampered["passed"] is False
    assert tampered["promotion_eligible"] is False


def test_event_provenance_recomputes_complete_revision_contract(tmp_path: Path) -> None:
    events = tmp_path / "invalid_events.csv"
    events.write_text(
        "timestamp,currency,impact,name,occurrence_id,revision,effective_from,effective_to,"
        "is_tombstone,identity_quality,recorded_at\n"
        "2026-07-20T08:30:00Z,USD,high,CPI,provider:1,2,2026-07-19T00:00:00Z,,"
        "false,source,2026-07-19 00:00:00\n",
        encoding="utf-8",
    )

    status = _event_provenance_status(events)

    assert status["pit_revision_contract"] is False
    assert status["promotion_eligible"] is False
    assert any("recorded_at" in reason for reason in status["limitations"])


def test_event_provenance_rejects_future_vintage_clocks_at_artifact_boundary(
    tmp_path: Path,
) -> None:
    events = tmp_path / "future_events.csv"
    events.write_text(
        "timestamp,currency,impact,name,occurrence_id,revision,effective_from,effective_to,"
        "is_tombstone,identity_quality,recorded_at\n"
        "2099-01-02T08:30:00Z,USD,high,CPI,provider:1,1,2099-01-01T00:00:00Z,,"
        "false,source,2099-01-01T00:00:00Z\n",
        encoding="utf-8",
    )

    status = _event_provenance_status(
        events,
        evaluated_at=datetime(2026, 7, 13, tzinfo=UTC),
    )

    assert status["pit_revision_contract"] is False
    assert status["promotion_eligible"] is False
    assert any("artifact creation time" in reason for reason in status["limitations"])


def test_event_provenance_allows_future_scheduled_event_known_before_boundary(
    tmp_path: Path,
) -> None:
    events = tmp_path / "scheduled_events.csv"
    events.write_text(
        "timestamp,currency,impact,name,occurrence_id,revision,effective_from,effective_to,"
        "is_tombstone,identity_quality,recorded_at\n"
        "2026-07-20T08:30:00Z,USD,high,CPI,provider:1,1,2026-07-12T00:00:00Z,,"
        "false,source,2026-07-12T00:00:00Z\n",
        encoding="utf-8",
    )

    status = _event_provenance_status(
        events,
        evaluated_at=datetime(2026, 7, 13, tzinfo=UTC),
    )

    assert status["pit_revision_contract"] is True
    assert status["stable_occurrence_identity"] is True
    assert status["promotion_eligible"] is True


def test_dataset_provenance_rejects_zero_ohlc_and_future_clocks(tmp_path: Path) -> None:
    input_path = tmp_path / "future_zero.csv"
    timestamps = ["2099-01-01T00:00:00Z", "2099-01-01T01:00:00Z"]
    pd.DataFrame(
        {
            "timestamp": timestamps,
            "source_time": timestamps,
            "ingested_time": timestamps,
            "available_time": timestamps,
            "open": 0.0,
            "high": 0.0,
            "low": 0.0,
            "close": 0.0,
        }
    ).to_csv(input_path, index=False)
    data_hash = hashlib.sha256(input_path.read_bytes()).hexdigest()
    sidecar = Path(str(input_path.resolve()) + ".provenance.json")
    sidecar.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "dataset_id": "future-zero",
                "synthetic": False,
                "promotion_use_approved": True,
                "data_sha256": data_hash,
                "source": {"name": "Test", "uri": "https://example.test/feed"},
                "license": {
                    "name": "Test",
                    "uri": "https://example.test/license",
                    "allows_model_research": True,
                },
                "acquired_at": "2099-01-02T00:00:00Z",
                "point_in_time": {
                    "event_time_column": "timestamp",
                    "source_time_column": "source_time",
                    "ingested_time_column": "ingested_time",
                    "available_time_column": "available_time",
                    "timezone": "UTC",
                    "timestamp_semantics": "bar_close",
                    "revision_policy": "immutable",
                    "index_is_available_time": True,
                    "future_data_rejected": True,
                },
                "transformation_lineage": [
                    {
                        "step_id": "test",
                        "code_version": "test",
                        "input_sha256": data_hash,
                        "output_sha256": data_hash,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    status = _single_dataset_provenance_status(
        input_path,
        evaluated_at=datetime(2026, 7, 13, tzinfo=UTC),
    )

    assert status["promotion_eligible"] is False
    assert any("artifact creation time" in reason for reason in status["limitations"])
    assert "OHLC values must be positive" in status["limitations"]


def test_artifact_audit_rejects_future_creation_timestamp(tmp_path: Path) -> None:
    output_dir, _ = _write_test_run(tmp_path)
    manifest_path = output_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["created_at_utc"] = "2099-01-01T00:00:00Z"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    report = audit_run_artifacts(output_dir)

    assert report["passed"] is False
    assert "manifest created_at_utc is in the future" in report["errors"]
    assert report["promotion_eligible"] is False


@pytest.mark.parametrize(
    "column",
    ["spread_pips", "slippage_pips", "exit_spread_pips", "exit_slippage_pips"],
)
def test_artifact_audit_rejects_nonfinite_round_trip_costs(
    tmp_path: Path,
    column: str,
) -> None:
    output_dir, _ = _write_test_run(tmp_path)
    trade_path = output_dir / "trade_log.csv"
    trades = pd.read_csv(trade_path)
    trades.loc[0, column] = float("nan")
    trades.to_csv(trade_path, index=False)
    _refresh_output_fingerprint(output_dir, "trade_log")

    report = audit_run_artifacts(output_dir)

    assert report["passed"] is False
    assert f"trade_log {column} must be finite" in report["errors"]


def test_artifact_audit_rejects_non_boolean_qa_passed_values(tmp_path: Path) -> None:
    output_dir, _ = _write_test_run(tmp_path)
    qa_path = output_dir / "data_qa.csv"
    qa = pd.read_csv(qa_path)
    qa["passed"] = "unknown"
    qa.to_csv(qa_path, index=False)
    _refresh_output_fingerprint(output_dir, "data_qa")

    report = audit_run_artifacts(output_dir)

    assert report["passed"] is False
    assert "data QA passed column must contain only booleans" in report["errors"]


def test_artifact_audit_rejects_nested_nonfinite_metrics(tmp_path: Path) -> None:
    output_dir, _ = _write_test_run(tmp_path)
    metrics_path = output_dir / "metrics.json"
    metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
    serialized = json.dumps(metrics)
    metrics_path.write_text(
        serialized[:-1] + ', "nested": {"invalid": 1e999}}',
        encoding="utf-8",
    )
    _refresh_output_fingerprint(output_dir, "metrics")

    report = audit_run_artifacts(output_dir)

    assert report["passed"] is False
    assert any("metrics" in error and "finite" in error for error in report["errors"])


def test_artifact_audit_rejects_nonfinite_equity_even_with_refreshed_hash(tmp_path: Path) -> None:
    output_dir, _ = _write_test_run(tmp_path)
    equity_path = output_dir / "equity_curve.csv"
    equity = pd.read_csv(equity_path)
    equity.loc[0, "equity"] = float("inf")
    equity.to_csv(equity_path, index=False)
    _refresh_output_fingerprint(output_dir, "equity_curve")

    report = audit_run_artifacts(output_dir)

    assert report["passed"] is False
    assert "equity_curve.equity[0] must be finite" in report["errors"]


def test_artifact_audit_requires_manifest_metrics_to_match_metrics_file(tmp_path: Path) -> None:
    output_dir, _ = _write_test_run(tmp_path)
    metrics_path = output_dir / "metrics.json"
    metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
    metrics["sharpe_ratio"] = float(metrics["sharpe_ratio"]) + 0.25
    metrics_path.write_text(json.dumps(metrics, allow_nan=False), encoding="utf-8")
    _refresh_output_fingerprint(output_dir, "metrics")

    report = audit_run_artifacts(output_dir)

    assert report["passed"] is False
    assert "manifest metrics do not match metrics.json" in report["errors"]


def test_artifact_audit_rejects_non_numeric_equity_even_with_refreshed_hash(
    tmp_path: Path,
) -> None:
    output_dir, _ = _write_test_run(tmp_path)
    equity_path = output_dir / "equity_curve.csv"
    equity = pd.read_csv(equity_path)
    equity["equity"] = equity["equity"].astype(object)
    equity.loc[0, "equity"] = "not-a-number"
    equity.to_csv(equity_path, index=False)
    _refresh_output_fingerprint(output_dir, "equity_curve")

    report = audit_run_artifacts(output_dir)

    assert report["passed"] is False
    assert "equity_curve.equity must be numeric" in report["errors"]


def test_equity_writer_requires_aware_unique_monotonic_timestamps() -> None:
    naive = pd.DataFrame(
        {"equity": [100_000.0, 100_100.0]},
        index=pd.date_range("2026-07-13", periods=2, freq="h"),
    )
    with pytest.raises(ValueError, match="timezone-aware"):
        _validate_equity_curve(naive)

    duplicated = naive.copy()
    duplicated.index = pd.DatetimeIndex([pd.Timestamp("2026-07-13T00:00:00Z")] * 2)
    with pytest.raises(ValueError, match="unique and monotonic"):
        _validate_equity_curve(duplicated)
