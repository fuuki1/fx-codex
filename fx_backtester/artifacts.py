from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from datetime import datetime, UTC
from pathlib import Path
from typing import Any

import pandas as pd
from pandas.errors import EmptyDataError

import fx_backtester
from fx_backtester.engine import BacktestConfig, BacktestResult
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

    trade_log_path = destination / "trade_log.csv"
    equity_path = destination / "equity_curve.csv"
    metrics_path = destination / "metrics.json"
    config_path = destination / "config.json"
    qa_path = destination / "data_qa.csv"
    manifest_path = destination / "manifest.json"

    result.trades.reindex(columns=TRADE_LOG_COLUMNS).to_csv(trade_log_path, index=False)
    result.equity_curve.to_csv(equity_path)
    metrics_path.write_text(
        json.dumps(_json_safe(result.metrics), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    config_path.write_text(
        json.dumps(_to_jsonable(config), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    qa_report = validate_price_data(data, qa_config)
    qa_report.to_csv(qa_path, index=False)

    created_at = datetime.now(UTC)
    repository_root = Path(__file__).resolve().parents[1]
    source_ledger = repository_root / "docs" / "research" / "SOURCE_LEDGER.md"
    data_start = min(frame.index.min() for frame in data.values()) if data else None
    data_end = max(frame.index.max() for frame in data.values()) if data else None
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
            "params": strategy_params,
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
            "trade_log": str(trade_log_path),
            "equity_curve": str(equity_path),
            "metrics": str(metrics_path),
            "config": str(config_path),
            "data_qa": str(qa_path),
        },
        "output_fingerprints": {
            "trade_log": _file_fingerprint(str(trade_log_path)),
            "equity_curve": _file_fingerprint(str(equity_path)),
            "metrics": _file_fingerprint(str(metrics_path)),
            "config": _file_fingerprint(str(config_path)),
            "data_qa": _file_fingerprint(str(qa_path)),
        },
        "quality_gates": {
            "qa_passed": bool(qa_report["passed"].all()) if not qa_report.empty else False,
            "required_trade_log_columns": list(REQUIRED_TRADE_LOG_COLUMNS),
            "trade_log_columns_present": [
                column for column in REQUIRED_TRADE_LOG_COLUMNS if column in result.trades.columns
            ],
        },
        "metrics": _json_safe(result.metrics),
    }
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

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
    for name, path in required_files.items():
        if not path.exists():
            errors.append(f"missing {name}: {path}")

    if not errors:
        manifest = json.loads(required_files["manifest"].read_text(encoding="utf-8"))
        qa_report = pd.read_csv(required_files["data_qa"])
        try:
            trades = pd.read_csv(required_files["trade_log"])
        except EmptyDataError:
            trades = pd.DataFrame()
            errors.append("trade_log is empty and missing headers")
        metrics = json.loads(required_files["metrics"].read_text(encoding="utf-8"))

        if qa_report.empty or not bool(qa_report["passed"].all()):
            errors.append("data QA did not pass")
        missing_trade_columns = [
            column for column in REQUIRED_TRADE_LOG_COLUMNS if column not in trades.columns
        ]
        if missing_trade_columns:
            errors.append(f"trade_log missing columns: {missing_trade_columns}")
        if not trades.empty:
            if bool((trades["spread_pips"].astype(float) <= 0).any()):
                errors.append("trade_log spread_pips must be positive")
            if bool((trades["slippage_pips"].astype(float) <= 0).any()):
                errors.append("trade_log slippage_pips must be positive")
        if "trade_count" in metrics and int(metrics["trade_count"]) != len(trades):
            errors.append("metrics trade_count does not match trade_log row count")
        if "schema_version" not in manifest:
            errors.append("manifest missing schema_version")
        if "inputs" not in manifest:
            errors.append("manifest missing input fingerprints")
        if trades.empty:
            warnings.append("trade_log is empty")

    return {
        "run_dir": str(directory),
        "passed": not errors,
        "errors": errors,
        "warnings": warnings,
    }


def _file_fingerprint(path: str | None) -> dict[str, Any] | None:
    if path is None:
        return None
    file_path = Path(path)
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


def _json_safe(values: dict[str, Any]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, value in values.items():
        if isinstance(value, float) and value == float("inf"):
            output[key] = "inf"
        else:
            output[key] = value
    return output


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
