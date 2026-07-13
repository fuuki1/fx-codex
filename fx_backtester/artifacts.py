from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import asdict, is_dataclass
from datetime import datetime, UTC
from math import isfinite
from numbers import Real
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from pandas.errors import EmptyDataError

import fx_backtester
from fx_backtester.engine import BacktestConfig, BacktestResult
from fx_backtester.data import EVENT_PIT_COLUMNS, load_economic_events_csv
from fx_backtester.qa import DataQualityConfig, validate_price_data
from fx_backtester.models import TRADE_LOG_COLUMNS
from fx_backtester.validation import REQUIRED_TRADE_LOG_COLUMNS


def write_backtest_run_artifacts(
    output_dir: str | Path,
    *,
    data_paths: list[str],
    events_path: str | None,
    strategy_name: str,
    strategy_params: dict[str, Any],
    config: BacktestConfig,
    result: BacktestResult,
    data: dict[str, pd.DataFrame],
    command: str,
    qa_config: DataQualityConfig | None = None,
) -> dict[str, Path]:
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    existing = tuple(destination.iterdir())
    if existing:
        raise FileExistsError(
            f"run artifact directory must be empty and immutable once written: {destination}"
        )
    reservation = destination / ".artifact-write.lock"
    reservation_fd = os.open(reservation, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(reservation_fd)

    trade_log_path = destination / "trade_log.csv"
    equity_path = destination / "equity_curve.csv"
    metrics_path = destination / "metrics.json"
    config_path = destination / "config.json"
    qa_path = destination / "data_qa.csv"
    manifest_path = destination / "manifest.json"

    try:
        created_at = datetime.now(UTC)
        metrics_payload = _finite_json_value(
            result.metrics,
            "metrics",
            numeric_leaves_only=True,
        )
        config_payload = _finite_json_value(_to_jsonable(config), "config")
        strategy_params_payload = _finite_json_value(strategy_params, "strategy_params")
        _validate_equity_curve(result.equity_curve)
        result.trades.reindex(columns=TRADE_LOG_COLUMNS).to_csv(trade_log_path, index=False)
        result.equity_curve.to_csv(equity_path)
        metrics_path.write_text(
            json.dumps(metrics_payload, indent=2, ensure_ascii=False, allow_nan=False),
            encoding="utf-8",
        )
        config_path.write_text(
            json.dumps(config_payload, indent=2, ensure_ascii=False, allow_nan=False),
            encoding="utf-8",
        )
        qa_report = validate_price_data(data, qa_config, as_of=created_at)
        qa_report.to_csv(qa_path, index=False)

        repository_root = Path(__file__).resolve().parents[1]
        source_ledger = repository_root / "docs" / "research" / "SOURCE_LEDGER.md"
        data_start = min(frame.index.min() for frame in data.values()) if data else None
        data_end = max(frame.index.max() for frame in data.values()) if data else None
        event_provenance = _event_provenance_status(
            events_path,
            evaluated_at=created_at,
        )
        dataset_provenance = _dataset_provenance_status(
            data_paths,
            evaluated_at=created_at,
        )
        qa_passed = bool(qa_report["passed"].all()) if not qa_report.empty else False
        manifest = {
            "schema_version": 2,
            "experiment_id": f"{destination.name}-{created_at.strftime('%Y%m%dT%H%M%S%fZ')}",
            "created_at_utc": created_at.isoformat(),
            "git": _git_provenance(repository_root),
            "package": {
                "name": "fx_backtester",
                "version": fx_backtester.__version__,
            },
            "runtime": {
                "python": sys.version,
                "platform": platform.platform(),
            },
            "command": command,
            "strategy": {
                "name": strategy_name,
                "params": strategy_params_payload,
            },
            "random_seed": _find_seed(strategy_params),
            "versions": {
                "dataset": [_file_fingerprint(path) for path in data_paths],
                "features": "strategy-defined; no standalone feature artifact",
                "labels": "trade-log-v1",
                "model": strategy_name,
            },
            "windows": {
                "data_start": _json_timestamp(data_start),
                "data_end": _json_timestamp(data_end),
                "train": None,
                "tune": None,
                "calibration": None,
                "test": None,
                "lockbox": None,
                "note": "Deterministic backtest run; ML promotion requires a separate five-way manifest.",
            },
            "cost_assumptions": _to_jsonable(config.execution),
            "inputs": {
                "data": [_file_fingerprint(path) for path in data_paths],
                "events": _file_fingerprint(events_path) if events_path else None,
                "dependency_definition": _file_fingerprint(str(repository_root / "pyproject.toml")),
                "source_ledger": (
                    _file_fingerprint(str(source_ledger)) if source_ledger.exists() else None
                ),
            },
            "outputs": {
                "trade_log": str(trade_log_path.resolve()),
                "equity_curve": str(equity_path.resolve()),
                "metrics": str(metrics_path.resolve()),
                "config": str(config_path.resolve()),
                "data_qa": str(qa_path.resolve()),
            },
            "output_fingerprints": {
                "trade_log": _file_fingerprint(str(trade_log_path)),
                "equity_curve": _file_fingerprint(str(equity_path)),
                "metrics": _file_fingerprint(str(metrics_path)),
                "config": _file_fingerprint(str(config_path)),
                "data_qa": _file_fingerprint(str(qa_path)),
            },
            "quality_gates": {
                "qa_passed": qa_passed,
                "event_provenance": event_provenance,
                "dataset_provenance": dataset_provenance,
                "promotion_eligible": bool(
                    qa_passed
                    and event_provenance["promotion_eligible"]
                    and dataset_provenance["promotion_eligible"]
                ),
                "required_trade_log_columns": list(REQUIRED_TRADE_LOG_COLUMNS),
                "trade_log_columns_present": [
                    column
                    for column in REQUIRED_TRADE_LOG_COLUMNS
                    if column in result.trades.columns
                ],
            },
            "metrics": metrics_payload,
        }
        manifest = _finite_json_value(manifest, "manifest")
        manifest_path.write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False, allow_nan=False),
            encoding="utf-8",
        )
    finally:
        reservation.unlink(missing_ok=True)

    return {
        "trade_log": trade_log_path,
        "equity_curve": equity_path,
        "metrics": metrics_path,
        "config": config_path,
        "data_qa": qa_path,
        "manifest": manifest_path,
    }


def audit_run_artifacts(output_dir: str | Path) -> dict[str, Any]:
    directory = Path(output_dir)
    required_files = {
        "manifest": directory / "manifest.json",
        "trade_log": directory / "trade_log.csv",
        "equity_curve": directory / "equity_curve.csv",
        "metrics": directory / "metrics.json",
        "config": directory / "config.json",
        "data_qa": directory / "data_qa.csv",
    }
    errors: list[str] = []
    warnings: list[str] = []
    promotion_eligible = False
    artifact_created_at: pd.Timestamp | None = None
    for name, path in required_files.items():
        if not path.is_file():
            errors.append(f"missing {name}: {path}")

    if not errors:
        manifest = _read_json_object(required_files["manifest"], "manifest", errors)
        metrics = _read_json_object(required_files["metrics"], "metrics", errors)
        config = _read_json_object(required_files["config"], "config", errors)
        if metrics is not None:
            _audit_finite_json_value(metrics, "metrics", errors, numeric_leaves_only=True)
        if config is not None:
            _audit_finite_json_value(config, "config", errors)
        if manifest is not None:
            _audit_finite_json_value(manifest, "manifest", errors)
            schema_version = manifest.get("schema_version")
            if schema_version != 2:
                errors.append(f"unsupported manifest schema_version: {schema_version!r}")
            else:
                _audit_schema_two_fingerprints(manifest, required_files, errors)
                artifact_created_at = _audit_artifact_created_at(manifest, errors)

        try:
            qa_report = pd.read_csv(required_files["data_qa"])
        except (OSError, ValueError) as error:
            qa_report = pd.DataFrame()
            errors.append(f"data_qa could not be read: {error}")
        try:
            trades = pd.read_csv(required_files["trade_log"])
        except EmptyDataError:
            trades = pd.DataFrame()
            errors.append("trade_log is empty and missing headers")
        except (OSError, ValueError) as error:
            trades = pd.DataFrame()
            errors.append(f"trade_log could not be read: {error}")
        try:
            equity_curve = pd.read_csv(required_files["equity_curve"])
        except (OSError, ValueError, EmptyDataError) as error:
            equity_curve = pd.DataFrame()
            errors.append(f"equity_curve could not be read: {error}")
        if equity_curve.empty:
            errors.append("equity_curve is empty")
        else:
            _audit_equity_curve(equity_curve, errors)

        if qa_report.empty or "passed" not in qa_report.columns:
            errors.append("data QA did not pass")
        elif not all(type(value) in (bool, np.bool_) for value in qa_report["passed"]):
            errors.append("data QA passed column must contain only booleans")
        elif not bool(qa_report["passed"].all()):
            errors.append("data QA did not pass")
        missing_trade_columns = [
            column for column in REQUIRED_TRADE_LOG_COLUMNS if column not in trades.columns
        ]
        if missing_trade_columns:
            errors.append(f"trade_log missing columns: {missing_trade_columns}")
        if not trades.empty:
            for column in (
                "spread_pips",
                "slippage_pips",
                "exit_spread_pips",
                "exit_slippage_pips",
            ):
                if column not in trades.columns:
                    continue
                values = _finite_numeric_column(trades, column, errors)
                if values is None:
                    continue
                if bool((values <= 0).any()):
                    errors.append(f"trade_log {column} must be positive")
            for column in (
                "units",
                "entry_price",
                "exit_price",
                "initial_risk_usd",
            ):
                if column not in trades.columns:
                    continue
                values = _finite_numeric_column(trades, column, errors)
                if values is not None and bool((values <= 0).any()):
                    errors.append(f"trade_log {column} must be positive")
            for column in ("gross_pnl", "fees", "net_pnl", "r_multiple"):
                if column not in trades.columns:
                    continue
                values = _finite_numeric_column(trades, column, errors)
                if column == "fees" and values is not None and bool((values < 0).any()):
                    errors.append("trade_log fees must be non-negative")
        if metrics is not None and "trade_count" in metrics:
            raw_trade_count = metrics["trade_count"]
            if not isinstance(raw_trade_count, int) or isinstance(raw_trade_count, bool):
                errors.append("metrics trade_count must be an integer")
            else:
                trade_count = raw_trade_count
                if trade_count != len(trades):
                    errors.append("metrics trade_count does not match trade_log row count")
        if manifest is not None and metrics is not None and manifest.get("metrics") != metrics:
            errors.append("manifest metrics do not match metrics.json")
        if manifest is not None:
            quality_gates = manifest.get("quality_gates")
            if not isinstance(quality_gates, Mapping):
                errors.append("manifest quality_gates must be an object")
            else:
                recorded_qa = quality_gates.get("qa_passed")
                actual_qa = (
                    bool(qa_report["passed"].all())
                    if not qa_report.empty and "passed" in qa_report.columns
                    else False
                )
                if not isinstance(recorded_qa, bool) or recorded_qa != actual_qa:
                    errors.append("manifest quality_gates.qa_passed does not match data_qa.csv")
                recorded_columns = quality_gates.get("trade_log_columns_present")
                actual_columns = [
                    column for column in REQUIRED_TRADE_LOG_COLUMNS if column in trades.columns
                ]
                if recorded_columns != actual_columns:
                    errors.append("manifest trade_log_columns_present does not match trade_log.csv")
                inputs = manifest.get("inputs")
                event_fingerprint = inputs.get("events") if isinstance(inputs, Mapping) else None
                event_path = (
                    str(event_fingerprint.get("path"))
                    if isinstance(event_fingerprint, Mapping)
                    and isinstance(event_fingerprint.get("path"), str)
                    else None
                )
                actual_event_provenance = _event_provenance_status(
                    event_path,
                    evaluated_at=artifact_created_at,
                )
                if quality_gates.get("event_provenance") != actual_event_provenance:
                    errors.append(
                        "manifest quality_gates.event_provenance does not match events input"
                    )
                data_fingerprints = inputs.get("data") if isinstance(inputs, Mapping) else None
                data_paths = (
                    [
                        str(fingerprint.get("path"))
                        for fingerprint in data_fingerprints
                        if isinstance(fingerprint, Mapping)
                        and isinstance(fingerprint.get("path"), str)
                    ]
                    if isinstance(data_fingerprints, list)
                    else []
                )
                actual_dataset_provenance = _dataset_provenance_status(
                    data_paths,
                    evaluated_at=artifact_created_at,
                )
                if quality_gates.get("dataset_provenance") != actual_dataset_provenance:
                    errors.append(
                        "manifest quality_gates.dataset_provenance does not match data inputs"
                    )
                recorded_promotion = quality_gates.get("promotion_eligible")
                expected_promotion = bool(
                    actual_qa
                    and actual_event_provenance["promotion_eligible"]
                    and actual_dataset_provenance["promotion_eligible"]
                )
                if not isinstance(recorded_promotion, bool) or (
                    recorded_promotion != expected_promotion
                ):
                    errors.append(
                        "manifest quality_gates.promotion_eligible does not match audited gates"
                    )
                else:
                    promotion_eligible = recorded_promotion
                if not expected_promotion:
                    warnings.append("run is not promotion-eligible")
        if manifest is not None and "schema_version" not in manifest:
            errors.append("manifest missing schema_version")
        if manifest is not None and "inputs" not in manifest:
            errors.append("manifest missing input fingerprints")
        if trades.empty:
            warnings.append("trade_log is empty")

    return {
        "run_dir": str(directory),
        "passed": not errors,
        "promotion_eligible": bool(not errors and promotion_eligible),
        "errors": errors,
        "warnings": warnings,
    }


def _event_provenance_status(
    path: str | Path | None,
    *,
    evaluated_at: object | None = None,
) -> dict[str, Any]:
    if path is None:
        return {
            "provided": False,
            "pit_revision_contract": False,
            "stable_occurrence_identity": False,
            "promotion_eligible": False,
            "limitations": ["event input was not provided; event-risk evaluation unavailable"],
        }
    limitations: list[str] = []
    try:
        frame = pd.read_csv(path)
    except (OSError, ValueError, EmptyDataError) as error:
        return {
            "provided": True,
            "pit_revision_contract": False,
            "stable_occurrence_identity": False,
            "promotion_eligible": False,
            "limitations": [f"event input cannot be verified: {error}"],
        }
    frame.columns = [str(column).strip().lower() for column in frame.columns]
    columns = {str(column).strip().lower() for column in frame.columns}
    contract = set(EVENT_PIT_COLUMNS).issubset(columns)
    if not contract:
        limitations.append("event input lacks the PIT revision contract")
    evaluation_boundary = _provenance_timestamp(
        evaluated_at if evaluated_at is not None else datetime.now(UTC)
    )
    if evaluation_boundary is None:
        limitations.append("event provenance evaluation boundary must be timezone-aware")
    validated_contract = False
    stable_identity = False
    if contract and frame.empty:
        limitations.append("event input is empty")
    elif contract:
        recorded_values = [_provenance_timestamp(value) for value in frame["recorded_at"]]
        effective_from_values = [_provenance_timestamp(value) for value in frame["effective_from"]]
        effective_to_values = [
            _provenance_timestamp(value)
            for value in frame["effective_to"]
            if not pd.isna(value) and str(value).strip()
        ]
        vintage_clocks = [*recorded_values, *effective_from_values, *effective_to_values]
        if any(value is None for value in recorded_values):
            limitations.append("event recorded_at contains a missing or naive timestamp")
        if any(value is None for value in effective_from_values):
            limitations.append("event effective_from contains a missing or naive timestamp")
        if any(value is None for value in effective_to_values):
            limitations.append("event effective_to contains a naive or invalid timestamp")
        if evaluation_boundary is not None and any(
            value is not None and value > evaluation_boundary for value in vintage_clocks
        ):
            limitations.append(
                "event recorded/effective vintage clock is later than the artifact creation time"
            )
        clocks_valid = not limitations
        if all(value is not None for value in recorded_values):
            assert all(value is not None for value in recorded_values)
            try:
                validated = load_economic_events_csv(
                    path,
                    as_of=(
                        evaluation_boundary
                        if evaluation_boundary is not None
                        else max(value for value in recorded_values if value is not None)
                    ),
                    require_point_in_time=True,
                )
            except (OSError, TypeError, ValueError, EmptyDataError) as error:
                limitations.append(f"event PIT revision contract is invalid: {error}")
            else:
                validated_contract = clocks_valid
                quality = validated["identity_quality"].astype(str).str.lower().str.strip()
                stable_identity = bool(not validated.empty and quality.eq("source").all())
                if not stable_identity:
                    limitations.append(
                        "source does not supply stable occurrence IDs for every event"
                    )
    return {
        "provided": True,
        "pit_revision_contract": validated_contract,
        "stable_occurrence_identity": stable_identity,
        "promotion_eligible": bool(validated_contract and stable_identity),
        "limitations": limitations,
    }


def _dataset_provenance_status(
    paths: Sequence[str | Path],
    *,
    evaluated_at: object | None = None,
) -> dict[str, Any]:
    inputs = [_single_dataset_provenance_status(path, evaluated_at=evaluated_at) for path in paths]
    limitations = [
        f"{entry['data_path']}: {limitation}"
        for entry in inputs
        for limitation in entry["limitations"]
    ]
    eligible = bool(inputs) and all(entry["promotion_eligible"] for entry in inputs)
    return {
        "contract_version": 1,
        "all_inputs_provenanced": eligible,
        "promotion_eligible": eligible,
        "inputs": inputs,
        "limitations": limitations,
    }


def _single_dataset_provenance_status(
    path: str | Path,
    *,
    evaluated_at: object | None = None,
) -> dict[str, Any]:
    data_path = Path(path).resolve()
    sidecar = Path(str(data_path) + ".provenance.json")
    base: dict[str, Any] = {
        "data_path": str(data_path),
        "sidecar_path": str(sidecar),
        "sidecar_fingerprint": None,
        "contract_valid": False,
        "promotion_eligible": False,
        "limitations": [],
    }
    limitations: list[str] = base["limitations"]
    if sidecar.is_symlink() or not sidecar.is_file():
        limitations.append("missing regular .provenance.json sidecar")
        return base
    try:
        base["sidecar_fingerprint"] = _file_fingerprint(str(sidecar))
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as error:
        limitations.append(f"sidecar cannot be verified: {error}")
        return base
    if not isinstance(payload, Mapping):
        limitations.append("sidecar must be a JSON object")
        return base

    data_fingerprint = _file_fingerprint(str(data_path))
    if payload.get("schema_version") != 1:
        limitations.append("unsupported provenance schema_version")
    if not _nonempty_string(payload.get("dataset_id")):
        limitations.append("dataset_id is required")
    if payload.get("synthetic") is not False:
        limitations.append("synthetic must be explicitly false")
    if payload.get("promotion_use_approved") is not True:
        limitations.append("promotion_use_approved must be explicitly true")
    if data_fingerprint is None or payload.get("data_sha256") != data_fingerprint["sha256"]:
        limitations.append("data_sha256 does not match the dataset")

    source = payload.get("source")
    if not isinstance(source, Mapping) or not all(
        _nonempty_string(source.get(field)) for field in ("name", "uri")
    ):
        limitations.append("source name and uri are required")
    license_contract = payload.get("license")
    if (
        not isinstance(license_contract, Mapping)
        or not all(_nonempty_string(license_contract.get(field)) for field in ("name", "uri"))
        or license_contract.get("allows_model_research") is not True
    ):
        limitations.append("license name/uri and allows_model_research=true are required")

    evaluation_boundary = _provenance_timestamp(
        evaluated_at if evaluated_at is not None else datetime.now(UTC)
    )
    if evaluation_boundary is None:
        limitations.append("artifact evaluation boundary must be timezone-aware")
    acquired_at = _provenance_timestamp(payload.get("acquired_at"))
    if acquired_at is None:
        limitations.append("acquired_at must be timezone-aware")
    elif evaluation_boundary is not None and acquired_at > evaluation_boundary:
        limitations.append("acquired_at is later than the artifact creation time")
    point_in_time = payload.get("point_in_time")
    required_time_fields = (
        "event_time_column",
        "source_time_column",
        "ingested_time_column",
        "available_time_column",
    )
    if not isinstance(point_in_time, Mapping):
        limitations.append("point_in_time contract is required")
    else:
        time_contract_limitations_start = len(limitations)
        declared_time_columns = [point_in_time.get(field) for field in required_time_fields]
        if not all(_nonempty_string(value) for value in declared_time_columns):
            limitations.append("point_in_time timestamp columns are required")
        elif len(set(declared_time_columns)) != len(required_time_fields):
            limitations.append(
                "point_in_time event/source/ingested/available roles require distinct columns"
            )
        if point_in_time.get("timezone") != "UTC":
            limitations.append("point_in_time timezone must be UTC")
        if point_in_time.get("timestamp_semantics") not in {
            "bar_close",
            "quote_time",
            "tick_time",
        }:
            limitations.append("point_in_time timestamp_semantics is invalid")
        if point_in_time.get("revision_policy") not in {"immutable", "append_only_vintages"}:
            limitations.append("point_in_time revision_policy is invalid")
        if point_in_time.get("index_is_available_time") is not True:
            limitations.append("index_is_available_time must be true")
        if point_in_time.get("future_data_rejected") is not True:
            limitations.append("future_data_rejected must be true")
        if len(limitations) == time_contract_limitations_start:
            _validate_dataset_time_lineage(
                data_path,
                point_in_time,
                acquired_at,
                evaluation_boundary,
                limitations,
            )

    lineage = payload.get("transformation_lineage")
    if not isinstance(lineage, list) or not lineage:
        limitations.append("transformation_lineage is required")
    else:
        for position, step in enumerate(lineage):
            if not isinstance(step, Mapping) or not all(
                _nonempty_string(step.get(field)) for field in ("step_id", "code_version")
            ):
                limitations.append(f"transformation_lineage[{position}] identity is invalid")
                continue
            if not _is_sha256(step.get("input_sha256")) or not _is_sha256(
                step.get("output_sha256")
            ):
                limitations.append(f"transformation_lineage[{position}] hashes are invalid")
        if (
            data_fingerprint is not None
            and isinstance(lineage[-1], Mapping)
            and lineage[-1].get("output_sha256") != data_fingerprint["sha256"]
        ):
            limitations.append("final transformation output_sha256 does not match dataset")

    base["contract_valid"] = not limitations
    base["promotion_eligible"] = not limitations
    return base


def _validate_dataset_time_lineage(
    path: Path,
    contract: Mapping[str, Any],
    acquired_at: pd.Timestamp | None,
    evaluated_at: pd.Timestamp | None,
    limitations: list[str],
) -> None:
    columns = [
        str(contract[field])
        for field in (
            "event_time_column",
            "source_time_column",
            "ingested_time_column",
            "available_time_column",
        )
    ]
    price_columns = ["open", "high", "low", "close"]
    read_columns = list(dict.fromkeys([*columns, *price_columns]))
    try:
        frame = pd.read_csv(path, usecols=read_columns)
    except (OSError, ValueError) as error:
        limitations.append(f"timestamp lineage columns cannot be read: {error}")
        return
    parsed: dict[str, pd.Series] = {}
    for column in columns:
        values: list[pd.Timestamp] = []
        for raw in frame[column].tolist():
            timestamp = _provenance_timestamp(raw)
            if timestamp is None:
                limitations.append(f"{column} contains a missing/naive timestamp")
                return
            values.append(timestamp)
        parsed[column] = pd.Series(values, index=frame.index)
    event, source, ingested, available = (parsed[column] for column in columns)
    if not bool((event == available).all()):
        limitations.append("dataset index/event time is earlier than available_time")
    if bool((source > available).any()) or bool((ingested > available).any()):
        limitations.append("available_time precedes source_time or ingested_time")
    if acquired_at is not None and not frame.empty and acquired_at < ingested.max():
        limitations.append("acquired_at precedes a row ingested_time")
    all_boundaries = pd.concat([event, source, ingested, available], ignore_index=True)
    if acquired_at is not None and not frame.empty and bool((all_boundaries > acquired_at).any()):
        limitations.append("dataset timestamp is later than acquired_at")
    if evaluated_at is not None and not frame.empty and bool((all_boundaries > evaluated_at).any()):
        limitations.append("dataset timestamp is later than artifact creation time")

    try:
        prices = frame[price_columns].apply(pd.to_numeric, errors="raise")
    except (TypeError, ValueError):
        limitations.append("OHLC values must be numeric")
        return
    price_values = prices.to_numpy(dtype=float)
    if not bool(np.isfinite(price_values).all()):
        limitations.append("OHLC values must be finite")
    if bool((prices <= 0).any().any()):
        limitations.append("OHLC values must be positive")


def _provenance_timestamp(value: object) -> pd.Timestamp | None:
    try:
        timestamp = pd.Timestamp(value)
    except (TypeError, ValueError):
        return None
    if pd.isna(timestamp) or timestamp.tzinfo is None or timestamp.utcoffset() is None:
        return None
    return timestamp.tz_convert("UTC")


def _audit_artifact_created_at(
    manifest: Mapping[str, Any],
    errors: list[str],
) -> pd.Timestamp | None:
    created_at = _provenance_timestamp(manifest.get("created_at_utc"))
    if created_at is None:
        errors.append("manifest created_at_utc must be timezone-aware")
        return None
    now = pd.Timestamp(datetime.now(UTC))
    if created_at > now + pd.Timedelta(seconds=5):
        errors.append("manifest created_at_utc is in the future")
        return None
    return created_at


def _nonempty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _finite_numeric_column(
    frame: pd.DataFrame,
    column: str,
    errors: list[str],
) -> pd.Series | None:
    try:
        values = pd.to_numeric(frame[column], errors="raise")
    except (TypeError, ValueError):
        errors.append(f"trade_log {column} must be numeric")
        return None
    if not bool(np.isfinite(values.to_numpy(dtype=float)).all()):
        errors.append(f"trade_log {column} must be finite")
        return None
    return values


def _read_json_object(
    path: Path,
    label: str,
    errors: list[str],
) -> dict[str, Any] | None:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            parse_constant=_reject_nonstandard_json_constant,
        )
    except (OSError, ValueError) as error:
        errors.append(f"{label} could not be read: {error}")
        return None
    if not isinstance(value, dict):
        errors.append(f"{label} must be a JSON object")
        return None
    return value


def _reject_nonstandard_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON numeric constant is forbidden: {value}")


def _finite_json_value(
    value: Any,
    label: str,
    *,
    numeric_leaves_only: bool = False,
) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _finite_json_value(
                item,
                f"{label}.{key}",
                numeric_leaves_only=numeric_leaves_only,
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [
            _finite_json_value(
                item,
                f"{label}[{position}]",
                numeric_leaves_only=numeric_leaves_only,
            )
            for position, item in enumerate(value)
        ]
    if isinstance(value, bool):
        if numeric_leaves_only:
            raise ValueError(f"{label} must be numeric, not boolean")
        return value
    if isinstance(value, Real):
        numeric = float(value)
        if not isfinite(numeric):
            raise ValueError(f"{label} must be finite")
        return int(value) if isinstance(value, (int, np.integer)) else numeric
    if value is None:
        if numeric_leaves_only:
            raise ValueError(f"{label} must be a finite numeric value")
        return None
    if numeric_leaves_only:
        raise ValueError(f"{label} must be a finite numeric value")
    if isinstance(value, (str, Path)):
        return str(value)
    if isinstance(value, (pd.Timestamp, pd.Timedelta, datetime)):
        if pd.isna(value):
            raise ValueError(f"{label} must not be missing")
        return value.isoformat()
    raise ValueError(f"{label} is not JSON serializable")


def _audit_finite_json_value(
    value: Any,
    label: str,
    errors: list[str],
    *,
    numeric_leaves_only: bool = False,
) -> None:
    try:
        _finite_json_value(value, label, numeric_leaves_only=numeric_leaves_only)
    except (TypeError, ValueError) as error:
        errors.append(str(error))


def _validate_equity_curve(frame: pd.DataFrame) -> None:
    if frame.empty:
        raise ValueError("equity_curve must not be empty")
    errors: list[str] = []
    _audit_equity_curve(frame, errors)
    if errors:
        raise ValueError("; ".join(errors))


def _audit_equity_curve(frame: pd.DataFrame, errors: list[str]) -> None:
    if "timestamp" in frame.columns:
        raw_timestamps = frame["timestamp"].tolist()
    elif isinstance(frame.index, pd.DatetimeIndex):
        raw_timestamps = frame.index.tolist()
    else:
        errors.append("equity_curve requires a timestamp column or DatetimeIndex")
        return
    timestamps: list[pd.Timestamp] = []
    for position, value in enumerate(raw_timestamps):
        try:
            timestamp = pd.Timestamp(value)
        except (TypeError, ValueError):
            errors.append(f"equity_curve.timestamp[{position}] must be a valid timestamp")
            return
        if pd.isna(timestamp) or timestamp.tzinfo is None or timestamp.utcoffset() is None:
            errors.append(f"equity_curve.timestamp[{position}] must be timezone-aware")
            return
        timestamps.append(timestamp.tz_convert("UTC"))
    timestamp_index = pd.DatetimeIndex(timestamps)
    if timestamp_index.has_duplicates or not timestamp_index.is_monotonic_increasing:
        errors.append("equity_curve timestamps must be unique and monotonic")
    if "equity" not in frame.columns:
        errors.append("equity_curve must contain an equity column")
    for column in frame.columns:
        if column == "timestamp":
            continue
        values = frame[column]
        if column.endswith("_locked") or column == "risk_locked":
            if not all(type(value) in (bool, np.bool_) for value in values.tolist()):
                errors.append(f"equity_curve.{column} must contain only booleans")
            continue
        if any(isinstance(value, (bool, np.bool_)) for value in values.tolist()):
            errors.append(f"equity_curve.{column} must be numeric, not boolean")
            continue
        try:
            numeric = pd.to_numeric(values, errors="raise")
        except (TypeError, ValueError):
            errors.append(f"equity_curve.{column} must be numeric")
            continue
        finite_mask = np.isfinite(numeric.to_numpy(dtype=float))
        if not bool(finite_mask.all()):
            position = int(np.flatnonzero(~finite_mask)[0])
            errors.append(f"equity_curve.{column}[{position}] must be finite")


def _audit_schema_two_fingerprints(
    manifest: Mapping[str, Any],
    required_files: Mapping[str, Path],
    errors: list[str],
) -> None:
    outputs = manifest.get("outputs")
    if not isinstance(outputs, Mapping):
        errors.append("manifest outputs must be an object")
    else:
        for name in ("trade_log", "equity_curve", "metrics", "config", "data_qa"):
            output_path = outputs.get(name)
            if not isinstance(output_path, str) or not output_path.strip():
                errors.append(f"outputs.{name} must be a non-empty path")
            elif Path(output_path).resolve() != required_files[name].resolve():
                errors.append(f"outputs.{name} does not point to the audited run file")

    output_fingerprints = manifest.get("output_fingerprints")
    if not isinstance(output_fingerprints, Mapping):
        errors.append("manifest output_fingerprints must be an object")
    else:
        for name in ("trade_log", "equity_curve", "metrics", "config", "data_qa"):
            _verify_recorded_fingerprint(
                f"output_fingerprints.{name}",
                output_fingerprints.get(name),
                errors,
                actual_path=required_files[name],
            )

    inputs = manifest.get("inputs")
    if not isinstance(inputs, Mapping):
        errors.append("manifest inputs must be an object")
        return
    data_fingerprints = inputs.get("data")
    if not isinstance(data_fingerprints, list) or not data_fingerprints:
        errors.append("inputs.data must be a non-empty fingerprint list")
    else:
        for position, fingerprint in enumerate(data_fingerprints):
            _verify_recorded_fingerprint(
                f"inputs.data[{position}]",
                fingerprint,
                errors,
            )

    events_fingerprint = inputs.get("events")
    if events_fingerprint is not None:
        _verify_recorded_fingerprint("inputs.events", events_fingerprint, errors)
    _verify_recorded_fingerprint(
        "inputs.dependency_definition",
        inputs.get("dependency_definition"),
        errors,
    )
    source_ledger_fingerprint = inputs.get("source_ledger")
    if source_ledger_fingerprint is not None:
        _verify_recorded_fingerprint(
            "inputs.source_ledger",
            source_ledger_fingerprint,
            errors,
        )

    versions = manifest.get("versions")
    if not isinstance(versions, Mapping):
        errors.append("manifest versions must be an object")
    elif versions.get("dataset") != data_fingerprints:
        errors.append("versions.dataset does not match inputs.data fingerprints")


def _verify_recorded_fingerprint(
    label: str,
    recorded: object,
    errors: list[str],
    *,
    actual_path: Path | None = None,
) -> None:
    if not isinstance(recorded, Mapping):
        errors.append(f"{label} must be a fingerprint object")
        return
    path_value = recorded.get("path")
    digest_value = recorded.get("sha256")
    bytes_value = recorded.get("bytes")
    if not isinstance(path_value, str) or not path_value.strip():
        errors.append(f"{label}.path must be a non-empty string")
        return
    if not _is_sha256(digest_value):
        errors.append(f"{label}.sha256 must be a lowercase SHA-256")
        return
    if not isinstance(bytes_value, int) or isinstance(bytes_value, bool) or bytes_value < 0:
        errors.append(f"{label}.bytes must be a non-negative integer")
        return

    if actual_path is not None and Path(path_value).resolve() != actual_path.resolve():
        errors.append(f"{label}.path does not point to the audited run file")
    fingerprint_path = actual_path if actual_path is not None else Path(path_value)
    if not fingerprint_path.is_file():
        errors.append(f"{label} source is not a regular file: {fingerprint_path}")
        return
    try:
        actual = _file_fingerprint(str(fingerprint_path))
    except OSError as error:
        errors.append(f"{label} could not be fingerprinted: {error}")
        return
    if actual is None:
        errors.append(f"{label} could not be fingerprinted")
        return
    if actual["sha256"] != digest_value:
        errors.append(f"{label} SHA-256 mismatch")
    if actual["bytes"] != bytes_value:
        errors.append(f"{label} byte-size mismatch")


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _file_fingerprint(path: str | None) -> dict[str, Any] | None:
    if path is None:
        return None
    file_path = Path(path).resolve()
    digest = hashlib.sha256()
    with file_path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return {
        "path": str(file_path),
        "sha256": digest.hexdigest(),
        "bytes": file_path.stat().st_size,
    }


def _to_jsonable(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return _to_jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(key): _to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    # pandas の Timedelta/Timestamp は JSON エンコーダが扱えないため ISO 文字列へ。
    # 例: BacktestConfig.pending_open_order_ttl (pd.Timedelta) を config.json へ書く。
    if isinstance(value, (pd.Timedelta, pd.Timestamp)):
        return value.isoformat()
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _git_provenance(repository_root: Path) -> dict[str, Any]:
    try:
        commit = _git_output(repository_root, "rev-parse", "HEAD")
        branch = _git_output(repository_root, "branch", "--show-current")
        status = _git_output(repository_root, "status", "--porcelain")
    except (OSError, subprocess.CalledProcessError) as error:
        return {"available": False, "error": str(error)}
    return {
        "available": True,
        "commit": commit,
        "branch": branch,
        "dirty_worktree": bool(status),
        "status_porcelain": status.splitlines(),
    }


def _git_output(repository_root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repository_root,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _find_seed(values: Mapping[str, Any]) -> int | None:
    for key, value in values.items():
        if "seed" in str(key).lower() and isinstance(value, int) and not isinstance(value, bool):
            return value
        if isinstance(value, Mapping):
            nested = _find_seed(value)
            if nested is not None:
                return nested
    return None


def _json_timestamp(value: object) -> str | None:
    if value is None or pd.isna(value):
        return None
    return pd.Timestamp(value).isoformat()
