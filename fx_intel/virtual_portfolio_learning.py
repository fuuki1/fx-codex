"""Fail-closed validation manifest for virtual-portfolio challenger research.

The daily target is deliberately excluded from both features and labels.  This
module does not train or promote a model: it proves whether the accumulated,
cost-adjusted point-in-time rows are eligible to enter an offline challenger
workflow with separate train, tune, calibration, test, and rolling holdout
windows. A fixed one-time lockbox remains a separate unavailable promotion gate.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
import hashlib
import json
import math
import random
from typing import Any

from fx_backtester.governance import PromotionEvidence, evaluate_promotion
from fx_intel.evaluation_labels import canonical_net_label_contract_flags
from fx_intel.virtual_portfolio_net_labels import VIRTUAL_LEARNING_CONTRACT

LEARNING_CONTRACT = VIRTUAL_LEARNING_CONTRACT
VALIDATION_CONTRACT = "virtual-portfolio-challenger-gate-v1"
MEMORY_CONTRACT = "virtual-portfolio-outcome-memory-v1"
VALIDATION_EVIDENCE_CONTRACT = "virtual-portfolio-validation-evidence-v1"
PROMOTION_AUDIT_CONTRACT = "virtual-portfolio-promotion-audit-v1"
SPLIT_NAMES = ("train", "tune", "calibration", "test", "lockbox")
SPLIT_WEIGHTS = (0.40, 0.133333, 0.133333, 0.20, 0.133334)
MINIMUM_SPLIT_ROWS = {
    "train": 60,
    "tune": 20,
    "calibration": 20,
    "test": 30,
    "lockbox": 20,
}
FORBIDDEN_TARGET_FEATURES = {
    "daily_target_jpy",
    "target_progress",
    "target_met",
    "remaining_daily_target_jpy",
}
# `label_available_time <= label_ingested_time` の段が実効か自明かを示す自己申告。
# `single_clock` は2値が同一なのでこの段が恒真であることを意味し、通過を
# 「取込ラグを検証した」と読んではいけない。生成は `_label_time_basis()`。
_LABEL_TIME_BASES = frozenset({"dual_clock", "single_clock", "unknown"})


class InvalidLearningDataset(ValueError):
    """Learning evidence is not admissible for challenger validation."""


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _aware(value: object, name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError) as error:
        raise InvalidLearningDataset(f"{name} must be an ISO timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise InvalidLearningDataset(f"{name} must be timezone-aware")
    return parsed.astimezone(UTC)


def _sha_field(row: Mapping[str, object], name: str) -> str:
    value = str(row.get(name) or "")
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise InvalidLearningDataset(f"{name} must be a lowercase SHA-256")
    return value


def _numeric(value: object, name: str) -> float:
    try:
        number = float(str(value))
    except (TypeError, ValueError) as error:
        raise InvalidLearningDataset(f"{name} must be numeric") from error
    if not math.isfinite(number):
        raise InvalidLearningDataset(f"{name} must be finite")
    return number


def _forbidden_feature_keys(value: object) -> set[str]:
    if isinstance(value, Mapping):
        found_mapping = FORBIDDEN_TARGET_FEATURES.intersection(str(key) for key in value)
        for nested in value.values():
            found_mapping.update(_forbidden_feature_keys(nested))
        return found_mapping
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        found_sequence: set[str] = set()
        for nested in value:
            found_sequence.update(_forbidden_feature_keys(nested))
        return found_sequence
    return set()


def _validate_attribution(row: Mapping[str, object]) -> None:
    attribution = row.get("attribution")
    if not isinstance(attribution, Mapping):
        raise InvalidLearningDataset("attribution must be an object")
    gross = int(_numeric(attribution.get("gross_market_pnl_jpy"), "gross P&L"))
    cost_fields = (
        "spread_quote_cost_jpy",
        "slippage_cost_jpy",
        "commission_jpy",
        "financing_jpy",
        "conversion_cost_jpy",
    )
    costs = [int(_numeric(attribution.get(field), field)) for field in cost_fields]
    if any(value < 0 for value in costs):
        raise InvalidLearningDataset("cost attribution cannot be negative")
    expected = gross - sum(costs)
    net = int(_numeric(row.get("net_pnl_jpy"), "net_pnl_jpy"))
    if expected != net or int(_numeric(attribution.get("net_pnl_jpy"), "attribution net")) != net:
        raise InvalidLearningDataset("cost-adjusted P&L attribution does not reconcile")


def _validate_canonical_net_label(row: Mapping[str, object]) -> None:
    canonical = row.get("canonical_outcome")
    if not isinstance(canonical, Mapping):
        raise InvalidLearningDataset("canonical_outcome must be an object")
    flags = canonical_net_label_contract_flags(canonical)
    if flags:
        raise InvalidLearningDataset(f"canonical net-R contract violation: {','.join(flags)}")
    stored_hash = str(row.get("canonical_outcome_sha256") or "")
    if stored_hash != _sha256(canonical):
        raise InvalidLearningDataset("canonical_outcome_sha256 mismatch")
    for name in (
        "decision_id",
        "symbol",
        "timeframe",
        "direction",
        "prediction_time",
        "realized_net_r",
        "label_version",
        "label_provenance",
        "cost_model_id",
    ):
        if row.get(name) != canonical.get(name):
            raise InvalidLearningDataset(f"canonical top-level identity mismatch: {name}")


def _validate_row(row: Mapping[str, object], *, as_of: datetime) -> dict[str, Any]:
    if row.get("contract") != LEARNING_CONTRACT:
        raise InvalidLearningDataset("learning contract mismatch")
    if row.get("research_only") is not True or row.get("promotion_eligible") is not False:
        raise InvalidLearningDataset("learning row must remain research-only")
    trade_id = str(row.get("trade_id") or "").strip()
    decision_id = str(row.get("decision_id") or "").strip()
    if not trade_id or not decision_id:
        raise InvalidLearningDataset("trade_id and decision_id are required")
    prediction_time = _aware(row.get("prediction_time"), "prediction_time")
    label_end_time = _aware(row.get("label_end_time"), "label_end_time")
    label_available_time = _aware(
        row.get("label_available_time"),
        "label_available_time",
    )
    label_ingested_time = _aware(
        row.get("label_ingested_time"),
        "label_ingested_time",
    )
    if not (
        prediction_time < label_end_time <= label_available_time <= label_ingested_time <= as_of
    ):
        raise InvalidLearningDataset("label interval is immature or reversed")
    raw_time_basis = row.get("label_time_basis")
    # 欠落を "unknown" へ黙って落とすと、収集契約を通っていない行が
    # 有効値として素通りする。省略と明示的な unknown は区別する。
    if not isinstance(raw_time_basis, str):
        raise InvalidLearningDataset("label_time_basis is required")
    label_time_basis = raw_time_basis
    if label_time_basis not in _LABEL_TIME_BASES:
        raise InvalidLearningDataset(f"unknown label_time_basis: {label_time_basis}")
    features = row.get("features")
    if not isinstance(features, Mapping):
        raise InvalidLearningDataset("features must be an object")
    model_inputs = {
        name: row.get(name)
        for name in (
            "features",
            "input_features",
            "input_feature_masks",
            "learning_dimensions",
            "input_context",
            "portfolio_risk_snapshot",
            "execution_snapshot",
        )
    }
    forbidden = _forbidden_feature_keys(model_inputs)
    if forbidden:
        raise InvalidLearningDataset(
            f"daily KPI leaked into model features: {sorted(forbidden)[0]}"
        )
    _sha_field(row, "data_sha256")
    _sha_field(row, "model_sha256")
    _sha_field(row, "policy_sha256")
    _sha_field(row, "decision_record_sha256")
    _sha_field(row, "open_record_sha256")
    _sha_field(row, "close_record_sha256")
    _sha_field(row, "close_observation_sha256")
    realized_net_r = _numeric(row.get("realized_net_r"), "realized_net_r")
    planned_risk = _numeric(row.get("planned_risk_jpy"), "planned_risk_jpy")
    net_pnl = _numeric(row.get("net_pnl_jpy"), "net_pnl_jpy")
    if planned_risk <= 0:
        raise InvalidLearningDataset("planned_risk_jpy must be positive")
    if not math.isclose(
        realized_net_r,
        net_pnl / planned_risk,
        rel_tol=1e-12,
        abs_tol=1e-12,
    ):
        raise InvalidLearningDataset("realized_net_r does not equal net_pnl_jpy / planned_risk_jpy")
    _validate_attribution(row)
    _validate_canonical_net_label(row)
    return {
        **dict(row),
        "_prediction_time": prediction_time,
        "_label_end_time": label_end_time,
        "_label_available_time": label_available_time,
        "_label_ingested_time": label_ingested_time,
        "_label_time_basis": label_time_basis,
    }


def prepare_temporal_learning_splits(
    learning_payload: Mapping[str, object],
    *,
    as_of: datetime,
    embargo_hours: int = 24,
) -> dict[str, list[dict[str, Any]]]:
    """Validate, order, purge, and embargo rows without opening the lockbox."""

    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise InvalidLearningDataset("as_of must be timezone-aware")
    if embargo_hours < 0:
        raise InvalidLearningDataset("embargo_hours cannot be negative")
    moment = as_of.astimezone(UTC)
    raw_rows = learning_payload.get("rows")
    if not isinstance(raw_rows, Sequence) or isinstance(raw_rows, (str, bytes)):
        raise InvalidLearningDataset("learning payload requires a rows list")
    validated: list[dict[str, Any]] = []
    seen: set[str] = set()
    seen_decisions: set[str] = set()
    for raw in raw_rows:
        if not isinstance(raw, Mapping):
            raise InvalidLearningDataset("every learning row must be an object")
        row = _validate_row(raw, as_of=moment)
        trade_id = str(row["trade_id"])
        if trade_id in seen:
            raise InvalidLearningDataset(f"duplicate trade_id: {trade_id}")
        decision_id = str(row["decision_id"])
        if decision_id in seen_decisions:
            raise InvalidLearningDataset(f"duplicate decision_id: {decision_id}")
        seen.add(trade_id)
        seen_decisions.add(decision_id)
        validated.append(row)
    declared_sha = str(learning_payload.get("dataset_sha256") or "")
    source_sha = _sha256([_public_row(row) for row in validated])
    if declared_sha and declared_sha != source_sha:
        raise InvalidLearningDataset("declared dataset hash does not match rows")
    validated.sort(key=lambda row: (row["_prediction_time"], str(row["trade_id"])))
    initial = _initial_splits(validated)
    cleaned, _audit = _purge_and_embargo(
        initial,
        embargo=timedelta(hours=embargo_hours),
    )
    return cleaned


def _initial_splits(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    total = len(rows)
    boundaries: list[int] = []
    running = 0.0
    for weight in SPLIT_WEIGHTS[:-1]:
        running += weight
        boundaries.append(round(total * running))
    slices = [0, *boundaries, total]
    return {name: rows[slices[index] : slices[index + 1]] for index, name in enumerate(SPLIT_NAMES)}


def _purge_and_embargo(
    splits: dict[str, list[dict[str, Any]]],
    *,
    embargo: timedelta,
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, object]]]:
    cleaned = {name: list(rows) for name, rows in splits.items()}
    audit: list[dict[str, object]] = []
    for left_name, right_name in zip(SPLIT_NAMES[:-1], SPLIT_NAMES[1:], strict=True):
        left = cleaned[left_name]
        right = cleaned[right_name]
        if not left or not right:
            continue
        right_start = right[0]["_prediction_time"]
        left_kept = [row for row in left if row["_label_end_time"] < right_start]
        purged_ids = [str(row["trade_id"]) for row in left if row not in left_kept]
        cleaned[left_name] = left_kept
        left_end = max(
            (row["_label_end_time"] for row in left_kept),
            default=right_start,
        )
        embargo_until = left_end + embargo
        right_kept = [row for row in right if row["_prediction_time"] > embargo_until]
        embargoed_ids = [str(row["trade_id"]) for row in right if row not in right_kept]
        cleaned[right_name] = right_kept
        audit.append(
            {
                "boundary": f"{left_name}->{right_name}",
                "right_start": right_start.isoformat(),
                "purged_trade_ids": purged_ids,
                "embargo_until": embargo_until.isoformat(),
                "embargoed_trade_ids": embargoed_ids,
            }
        )
    return cleaned, audit


def _public_row(row: Mapping[str, Any]) -> dict[str, object]:
    return {
        key: value
        for key, value in row.items()
        if key
        not in {
            "_prediction_time",
            "_label_end_time",
            "_label_available_time",
            "_label_ingested_time",
            "_label_time_basis",
        }
    }


def build_challenger_manifest(
    learning_payload: Mapping[str, object],
    *,
    as_of: datetime | None = None,
    minimum_rows: int = 150,
    embargo_hours: int = 24,
) -> dict[str, object]:
    """Build a deterministic validation gate without training or opening lockbox."""

    raw_moment = as_of or datetime.now(UTC)
    if raw_moment.tzinfo is None or raw_moment.utcoffset() is None:
        raise InvalidLearningDataset("as_of must be timezone-aware")
    moment = raw_moment.astimezone(UTC)
    if minimum_rows < sum(MINIMUM_SPLIT_ROWS.values()):
        raise InvalidLearningDataset("minimum_rows is smaller than required split minima")
    if embargo_hours < 0:
        raise InvalidLearningDataset("embargo_hours cannot be negative")
    raw_rows = learning_payload.get("rows")
    if not isinstance(raw_rows, Sequence) or isinstance(raw_rows, (str, bytes)):
        raise InvalidLearningDataset("learning payload requires a rows list")
    validated: list[dict[str, Any]] = []
    seen_trade_ids: set[str] = set()
    seen_decision_ids: set[str] = set()
    for raw in raw_rows:
        if not isinstance(raw, Mapping):
            raise InvalidLearningDataset("every learning row must be an object")
        row = _validate_row(raw, as_of=moment)
        trade_id = str(row["trade_id"])
        if trade_id in seen_trade_ids:
            raise InvalidLearningDataset(f"duplicate trade_id: {trade_id}")
        decision_id = str(row["decision_id"])
        if decision_id in seen_decision_ids:
            raise InvalidLearningDataset(f"duplicate decision_id: {decision_id}")
        seen_trade_ids.add(trade_id)
        seen_decision_ids.add(decision_id)
        validated.append(row)
    source_rows = [_public_row(row) for row in validated]
    source_dataset_sha = _sha256(source_rows)
    declared_sha = str(learning_payload.get("dataset_sha256") or "")
    if declared_sha and declared_sha != source_dataset_sha:
        raise InvalidLearningDataset("declared dataset hash does not match rows")
    validated.sort(key=lambda row: (row["_prediction_time"], str(row["trade_id"])))
    dataset_rows = [_public_row(row) for row in validated]
    ordered_dataset_sha = _sha256(dataset_rows)

    manifest: dict[str, object] = {
        "contract": VALIDATION_CONTRACT,
        "generated_at": moment.isoformat(),
        "source_contract": LEARNING_CONTRACT,
        "source_dataset_sha256": source_dataset_sha,
        "ordered_dataset_sha256": ordered_dataset_sha,
        "source_row_count": len(validated),
        "minimum_rows": minimum_rows,
        "daily_target_used_as_feature": False,
        "daily_target_used_as_label": False,
        "research_only": True,
        "promotion_eligible": False,
        "execution_target": "shadow_decision_only",
        "required_validation": {
            "temporal_protocol": "purged walk-forward plus CPCV",
            "separate_windows": list(SPLIT_NAMES),
            "all_trials_recorded": True,
            "pbo_required": True,
            "deflated_sharpe_required": True,
            "bootstrap_uncertainty_required": True,
            "baseline_comparisons_required": True,
            "cost_stress_multipliers": [1.5, 2.0, 3.0],
            "lockbox_policy": (
                "a separate fixed one-time lockbox is required; the fifth split "
                "in this rolling dataset is only a development holdout"
            ),
        },
    }
    if len(validated) < minimum_rows:
        manifest.update(
            {
                "status": "insufficient_data",
                "eligible_for_candidate_training": False,
                "reason": f"成熟PIT行が{len(validated)}/{minimum_rows}件",
                "splits": {},
                "boundary_audit": [],
            }
        )
        manifest["manifest_sha256"] = _sha256(manifest)
        return manifest

    initial = _initial_splits(validated)
    cleaned, boundary_audit = _purge_and_embargo(
        initial,
        embargo=timedelta(hours=embargo_hours),
    )
    split_payload: dict[str, object] = {}
    minima_pass = True
    for name in SPLIT_NAMES:
        rows = [_public_row(row) for row in cleaned[name]]
        count = len(rows)
        minimum = MINIMUM_SPLIT_ROWS[name]
        minima_pass = minima_pass and count >= minimum
        split_payload[name] = {
            "row_count": count,
            "minimum_rows": minimum,
            "passed": count >= minimum,
            "start_prediction_time": (rows[0]["prediction_time"] if rows else None),
            "end_label_time": rows[-1]["label_end_time"] if rows else None,
            "trade_ids_sha256": _sha256([row["trade_id"] for row in rows]),
            "sealed": False,
            "role": ("rolling_development_holdout" if name == "lockbox" else f"development_{name}"),
        }
    manifest.update(
        {
            "status": (
                "ready_for_candidate_training"
                if minima_pass
                else "insufficient_after_purge_embargo"
            ),
            "eligible_for_candidate_training": minima_pass,
            "reason": (
                "候補モデルのオフライン学習へ進める"
                if minima_pass
                else "purge/embargo後に必要な時系列窓件数を満たさない"
            ),
            "embargo_hours": embargo_hours,
            "splits": split_payload,
            "boundary_audit": boundary_audit,
            "lockbox_access": "unavailable_no_fixed_lockbox_commitment",
            "rolling_holdout_row_count": len(cleaned["lockbox"]),
        }
    )
    manifest["manifest_sha256"] = _sha256(manifest)
    return manifest


def _bootstrap_mean_ci(
    values: Sequence[float],
    *,
    seed: int,
    draws: int = 2_000,
) -> dict[str, object]:
    """Return a deterministic moving-block bootstrap interval."""

    if not values:
        return {
            "status": "unavailable",
            "reason": "test window has no rows",
            "draws": 0,
        }
    rng = random.Random(seed)
    count = len(values)
    block_length = max(1, round(math.sqrt(count)))
    samples: list[float] = []
    for _ in range(draws):
        selected: list[float] = []
        while len(selected) < count:
            start = rng.randrange(count)
            for offset in range(block_length):
                selected.append(float(values[(start + offset) % count]))
                if len(selected) == count:
                    break
        samples.append(sum(selected) / count)
    samples.sort()
    lower_index = max(0, math.floor(0.025 * (draws - 1)))
    upper_index = min(draws - 1, math.ceil(0.975 * (draws - 1)))
    return {
        "status": "available",
        "method": "moving_block_bootstrap",
        "seed": seed,
        "draws": draws,
        "block_length": block_length,
        "lower_95": samples[lower_index],
        "upper_95": samples[upper_index],
    }


def _stressed_net_r(row: Mapping[str, object], multiplier: float) -> float:
    attribution = row.get("attribution")
    if not isinstance(attribution, Mapping):
        raise InvalidLearningDataset("attribution must be an object")
    gross = _numeric(attribution.get("gross_market_pnl_jpy"), "gross P&L")
    costs = sum(
        _numeric(attribution.get(field), field)
        for field in (
            "spread_quote_cost_jpy",
            "slippage_cost_jpy",
            "commission_jpy",
            "financing_jpy",
            "conversion_cost_jpy",
        )
    )
    planned_risk = _numeric(row.get("planned_risk_jpy"), "planned_risk_jpy")
    if planned_risk <= 0:
        raise InvalidLearningDataset("planned_risk_jpy must be positive")
    return (gross - multiplier * costs) / planned_risk


def build_validation_evidence(
    learning_payload: Mapping[str, object],
    manifest: Mapping[str, object],
    *,
    as_of: datetime | None = None,
    seed: int = 20_260_730,
) -> dict[str, object]:
    """Compute admissible test-window evidence and keep unavailable gates closed.

    This is not a model trainer.  It evaluates realized, cost-adjusted outcomes
    only on the purged test window.  PBO, DSR, calibration, CPCV, and lockbox
    results stay unavailable until their required trial/model evidence exists.
    """

    raw_moment = as_of or datetime.now(UTC)
    if raw_moment.tzinfo is None or raw_moment.utcoffset() is None:
        raise InvalidLearningDataset("as_of must be timezone-aware")
    moment = raw_moment.astimezone(UTC)
    manifest_generated_at = _aware(
        manifest.get("generated_at"),
        "manifest.generated_at",
    )
    if manifest_generated_at > moment:
        raise InvalidLearningDataset("manifest was generated after evidence as_of")
    expected_manifest = build_challenger_manifest(
        learning_payload,
        as_of=manifest_generated_at,
        minimum_rows=int(_numeric(manifest.get("minimum_rows"), "minimum_rows")),
        embargo_hours=int(_numeric(manifest.get("embargo_hours") or 24, "embargo_hours")),
    )
    if _canonical_json(expected_manifest) != _canonical_json(manifest):
        raise InvalidLearningDataset(
            "manifest does not match the current dataset and temporal splits"
        )
    raw_rows = learning_payload.get("rows")
    if not isinstance(raw_rows, Sequence) or isinstance(raw_rows, (str, bytes)):
        raise InvalidLearningDataset("learning payload requires a rows list")
    validated: list[dict[str, Any]] = []
    seen: set[str] = set()
    seen_decisions: set[str] = set()
    for raw in raw_rows:
        if not isinstance(raw, Mapping):
            raise InvalidLearningDataset("every learning row must be an object")
        row = _validate_row(raw, as_of=moment)
        trade_id = str(row["trade_id"])
        if trade_id in seen:
            raise InvalidLearningDataset(f"duplicate trade_id: {trade_id}")
        decision_id = str(row["decision_id"])
        if decision_id in seen_decisions:
            raise InvalidLearningDataset(f"duplicate decision_id: {decision_id}")
        seen.add(trade_id)
        seen_decisions.add(decision_id)
        validated.append(row)
    validated.sort(key=lambda row: (row["_prediction_time"], str(row["trade_id"])))

    base: dict[str, object] = {
        "contract": VALIDATION_EVIDENCE_CONTRACT,
        "generated_at": moment.isoformat(),
        "source_dataset_sha256": str(
            expected_manifest.get("ordered_dataset_sha256")
            or learning_payload.get("dataset_sha256")
            or _sha256([_public_row(row) for row in validated])
        ),
        "manifest_sha256": str(expected_manifest.get("manifest_sha256") or ""),
        "source_row_count": len(validated),
        "research_only": True,
        "promotion_eligible": False,
        "execution_target": "shadow_decision_only",
        "lockbox_access": "unavailable_no_fixed_lockbox_commitment",
        "evaluation_scope": ("rolling development diagnostic; no fixed final test is committed"),
        "daily_target_used_as_feature": False,
        "daily_target_used_as_label": False,
    }
    if manifest.get("eligible_for_candidate_training") is not True:
        base.update(
            {
                "status": "insufficient_data",
                "reason": str(manifest.get("reason") or "candidate training gate is closed"),
                "test_window": {},
                "gates": {
                    "pit_integrity": "passed_for_available_rows",
                    "purged_test": "unavailable",
                    "bootstrap_ci": "unavailable",
                    "cost_stress": "unavailable",
                    "pbo": "unavailable_no_trial_matrix",
                    "deflated_sharpe": "unavailable_no_trial_ledger",
                    "calibration": "unavailable_no_probabilities",
                    "cpcv": "unavailable_no_candidate_trials",
                    "lockbox": "unavailable_no_fixed_lockbox_commitment",
                },
            }
        )
        base["evidence_sha256"] = _sha256(base)
        return base

    initial = _initial_splits(validated)
    cleaned, boundary_audit = _purge_and_embargo(
        initial,
        embargo=timedelta(
            hours=int(_numeric(manifest.get("embargo_hours") or 24, "embargo_hours"))
        ),
    )
    test_rows = cleaned["test"]
    net_rs = [float(row["realized_net_r"]) for row in test_rows]
    mean_net_r = sum(net_rs) / len(net_rs) if net_rs else None
    bootstrap = _bootstrap_mean_ci(net_rs, seed=seed)
    stress: dict[str, object] = {}
    for multiplier in (1.0, 1.5, 2.0, 3.0):
        values = [_stressed_net_r(row, multiplier) for row in test_rows]
        stress[f"{multiplier:.1f}x"] = {
            "row_count": len(values),
            "mean_net_r": (sum(values) / len(values) if values else None),
            "positive_mean": bool(values and sum(values) / len(values) > 0),
        }
    lower = bootstrap.get("lower_95")
    positive_ci = isinstance(lower, (int, float)) and float(lower) > 0
    two_x = stress["2.0x"]
    assert isinstance(two_x, Mapping)
    base.update(
        {
            "status": "blocked_missing_trial_and_calibration_evidence",
            "reason": (
                "purged test統計は計算済みだが、PBO/DSR/CPCV/較正済み候補モデルの証拠が無い"
            ),
            "test_window": {
                "role": "rolling_development_diagnostic",
                "row_count": len(test_rows),
                "mean_net_r": mean_net_r,
                "bootstrap_mean_net_r": bootstrap,
                "cost_stress": stress,
                "no_trade_baseline_mean_net_r": 0.0,
                "boundary_audit": boundary_audit,
            },
            "gates": {
                "pit_integrity": "passed",
                "purged_test": "passed" if test_rows else "failed_empty",
                "fixed_final_test": "unavailable_no_fixed_test_commitment",
                "positive_expectancy_lower_95": "passed" if positive_ci else "failed",
                "two_x_cost_positive": (
                    "passed" if two_x.get("positive_mean") is True else "failed"
                ),
                "pbo": "unavailable_no_trial_matrix",
                "deflated_sharpe": "unavailable_no_trial_ledger",
                "calibration": "unavailable_no_probabilities",
                "cpcv": "unavailable_no_candidate_trials",
                "lockbox": "unavailable_no_fixed_lockbox_commitment",
            },
        }
    )
    base["evidence_sha256"] = _sha256(base)
    return base


def build_outcome_memory(
    learning_payload: Mapping[str, object],
    *,
    as_of: datetime | None = None,
) -> dict[str, object]:
    """Aggregate explainable outcome memory without creating a trading model."""

    raw_moment = as_of or datetime.now(UTC)
    if raw_moment.tzinfo is None or raw_moment.utcoffset() is None:
        raise InvalidLearningDataset("as_of must be timezone-aware")
    moment = raw_moment.astimezone(UTC)
    raw_rows = learning_payload.get("rows")
    if not isinstance(raw_rows, Sequence) or isinstance(raw_rows, (str, bytes)):
        raise InvalidLearningDataset("learning payload requires a rows list")
    validated = []
    for raw in raw_rows:
        if not isinstance(raw, Mapping):
            raise InvalidLearningDataset("every learning row must be an object")
        validated.append(_validate_row(raw, as_of=moment))
    validated.sort(key=lambda row: (row["_label_end_time"], str(row["trade_id"])))
    segments: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    failure_counts: dict[str, int] = {}
    for row in validated:
        key = (
            str(row.get("symbol") or ""),
            str(row.get("timeframe") or ""),
            str(row.get("direction") or ""),
        )
        segments.setdefault(key, []).append(row)
        failures = row.get("failures")
        if isinstance(failures, Sequence) and not isinstance(failures, (str, bytes)):
            for failure in failures:
                if isinstance(failure, Mapping):
                    code = str(failure.get("code") or "unknown")
                    failure_counts[code] = failure_counts.get(code, 0) + 1
    segment_rows: list[dict[str, object]] = []
    for (symbol, timeframe, direction), rows in sorted(segments.items()):
        net_rs = [float(row["realized_net_r"]) for row in rows]
        net_pnls = [int(row["net_pnl_jpy"]) for row in rows]
        segment_rows.append(
            {
                "symbol": symbol,
                "timeframe": timeframe,
                "direction": direction,
                "closed_trade_count": len(rows),
                "win_count": sum(value > 0 for value in net_pnls),
                "loss_count": sum(value < 0 for value in net_pnls),
                "mean_net_r": sum(net_rs) / len(net_rs),
                "cumulative_net_pnl_jpy": sum(net_pnls),
                "last_five_net_r": net_rs[-5:],
                "latest_label_end_time": rows[-1]["label_end_time"],
            }
        )
    memory: dict[str, object] = {
        "contract": MEMORY_CONTRACT,
        "generated_at": moment.isoformat(),
        "source_contract": LEARNING_CONTRACT,
        "source_dataset_sha256": str(learning_payload.get("dataset_sha256") or ""),
        "source_row_count": len(validated),
        "segments": segment_rows,
        "failure_counts": dict(sorted(failure_counts.items())),
        "uses_only_mature_cost_adjusted_outcomes": True,
        "daily_target_used_as_feature": False,
        "daily_target_used_as_label": False,
        "application": (
            "deterministic safety veto only; challenger changes require separate validation"
        ),
        "research_only": True,
        "promotion_eligible": False,
    }
    memory["memory_sha256"] = _sha256(memory)
    return memory


def build_promotion_audit(
    learning_payload: Mapping[str, object],
    manifest: Mapping[str, object],
    validation_evidence: Mapping[str, object],
    *,
    git_commit: str | None,
    dirty_worktree: bool | None,
    as_of: datetime | None = None,
) -> dict[str, object]:
    """Evaluate the authoritative validated-stage gates without filling unknowns.

    Missing trial, calibration, lockbox, provenance, coverage, or incident
    evidence remains ``None`` and therefore fails closed in ``evaluate_promotion``.
    This artifact is a governance decision, never a model or policy mutation.
    """

    raw_moment = as_of or datetime.now(UTC)
    if raw_moment.tzinfo is None or raw_moment.utcoffset() is None:
        raise InvalidLearningDataset("as_of must be timezone-aware")
    moment = raw_moment.astimezone(UTC)
    raw_rows = learning_payload.get("rows")
    if not isinstance(raw_rows, Sequence) or isinstance(raw_rows, (str, bytes)):
        raise InvalidLearningDataset("learning payload requires a rows list")
    rows = [row for row in raw_rows if isinstance(row, Mapping)]
    if len(rows) != len(raw_rows):
        raise InvalidLearningDataset("every learning row must be an object")

    manifest_generated_at = _aware(
        manifest.get("generated_at"),
        "manifest.generated_at",
    )
    if manifest_generated_at > moment:
        raise InvalidLearningDataset("manifest was generated after audit as_of")
    expected_manifest = build_challenger_manifest(
        learning_payload,
        as_of=manifest_generated_at,
        minimum_rows=int(_numeric(manifest.get("minimum_rows"), "minimum_rows")),
        embargo_hours=int(_numeric(manifest.get("embargo_hours") or 24, "embargo_hours")),
    )
    if _canonical_json(expected_manifest) != _canonical_json(manifest):
        raise InvalidLearningDataset(
            "manifest does not match the current dataset and temporal splits"
        )
    evidence_generated_at = _aware(
        validation_evidence.get("generated_at"),
        "validation_evidence.generated_at",
    )
    if evidence_generated_at > moment:
        raise InvalidLearningDataset("validation evidence was generated after audit as_of")
    bootstrap_seed = 20_260_730
    test_window = validation_evidence.get("test_window")
    if isinstance(test_window, Mapping):
        bootstrap = test_window.get("bootstrap_mean_net_r")
        if isinstance(bootstrap, Mapping) and isinstance(bootstrap.get("seed"), int):
            bootstrap_seed = int(bootstrap["seed"])
    expected_evidence = build_validation_evidence(
        learning_payload,
        expected_manifest,
        as_of=evidence_generated_at,
        seed=bootstrap_seed,
    )
    if _canonical_json(expected_evidence) != _canonical_json(validation_evidence):
        raise InvalidLearningDataset(
            "validation evidence does not match the current dataset and manifest"
        )

    split_payload = expected_manifest.get("splits")
    splits_available = isinstance(split_payload, Mapping) and bool(split_payload)

    regimes: set[str] = set()
    pairs: set[str] = set()
    real_delayed_rows = 0
    for row in rows:
        symbol = str(row.get("symbol") or "").strip()
        if symbol:
            pairs.add(symbol)
        features = row.get("features")
        if isinstance(features, Mapping):
            regime = str(features.get("regime") or "").strip()
            if regime:
                regimes.add(regime)
        attribution = row.get("attribution")
        if isinstance(attribution, Mapping):
            replay_evidence = attribution.get("replay_evidence")
            if (
                attribution.get("execution_observation_mode") == "delayed_historical_bid_ask_replay"
                and isinstance(replay_evidence, Mapping)
                and replay_evidence.get("data_mode") == "delayed_historical_download"
            ):
                real_delayed_rows += 1

    governance_evidence = PromotionEvidence(
        dataset_hash=str(expected_manifest.get("ordered_dataset_sha256") or "") or None,
        git_commit=git_commit,
        dirty_worktree=dirty_worktree,
        synthetic_data=(False if rows and real_delayed_rows == len(rows) else None),
        point_in_time_violations=0,
        future_feature_violations=0,
        trial_count=None,
        sample_count=len(rows),
        net_expectancy_r=None,
        expectancy_ci_lower_r=None,
        dsr_probability=None,
        pbo_probability=None,
        max_drawdown_pct=None,
        brier_improvement=None,
        cost_stress_2x_expectancy_r=None,
        regime_count=len(regimes) if regimes else None,
        pair_count=len(pairs) if pairs else None,
        lockbox_evaluated_once=None,
        lockbox_reused_for_selection=None,
        major_operational_incidents=None,
        data_quality_incidents=None,
        calibration_window_separate=True if splits_available else None,
        test_window_separate=None,
        live_like_execution_validated=False,
    )
    report = evaluate_promotion(governance_evidence, target_stage="validated")
    payload: dict[str, object] = {
        "contract": PROMOTION_AUDIT_CONTRACT,
        "generated_at": moment.isoformat(),
        "source_dataset_sha256": governance_evidence.dataset_hash,
        "manifest_sha256": str(expected_manifest.get("manifest_sha256") or ""),
        "validation_evidence_sha256": str(expected_evidence.get("evidence_sha256") or ""),
        "current_stage": "research",
        "requested_stage": "validated",
        "decision": "remain_research" if not report.passed else "eligible_for_manual_review",
        "automatic_promotion": False,
        "promotion_eligible": report.passed,
        "execution_target": "shadow_decision_only",
        "broker_connected": False,
        "governance_evidence": asdict(governance_evidence),
        "promotion_report": report.to_dict(),
    }
    payload["promotion_audit_sha256"] = _sha256(payload)
    return payload


__all__ = [
    "FORBIDDEN_TARGET_FEATURES",
    "InvalidLearningDataset",
    "LEARNING_CONTRACT",
    "MEMORY_CONTRACT",
    "MINIMUM_SPLIT_ROWS",
    "PROMOTION_AUDIT_CONTRACT",
    "SPLIT_NAMES",
    "VALIDATION_CONTRACT",
    "build_challenger_manifest",
    "build_outcome_memory",
    "build_promotion_audit",
    "build_validation_evidence",
    "prepare_temporal_learning_splits",
]
