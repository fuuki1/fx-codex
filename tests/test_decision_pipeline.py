"""9段チェックリスト型 意思決定パイプライン(decision_pipeline.py)のテスト。

ネットワーク不要。各ゲートの ok/warn/block/skip の分岐と、コードに新規実装した
スプレッド確認・執行コスト控除・ポジションサイズ算出を検証する。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, UTC
import hashlib
import json
from pathlib import Path

import pytest

from fx_backtester.calibration import fit_calibrator

from fx_intel.decision_pipeline import (
    ATR_PCT_MAX,
    CalibratedProbabilityEvidence,
    SPREAD_BLOCK_FRACTION,
    calibrated_probability_from_artifact,
    _compute_calibration_metrics,
    estimate_expected_r,
    execution_cost_in_r,
    position_units,
    run_pipeline,
)
from fx_intel.sentiment import CurrencySentiment
from fx_intel.technicals import IntervalView, PairTechnicals

# 木曜・市場オープン。build_trade_plan/is_market_open と同じ扱い。
OPEN = datetime(2026, 7, 2, 8, 0, tzinfo=UTC)
# 土曜・市場休場。
CLOSED = datetime(2026, 7, 4, 8, 0, tzinfo=UTC)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _label_timing(
    timestamps: list[datetime],
    barrier_path: Path,
    *,
    horizon_seconds: float = 3600.0,
) -> dict[str, object]:
    digest = _sha256(barrier_path)
    label_end = [timestamp + timedelta(seconds=horizon_seconds) for timestamp in timestamps]
    return {
        "prediction_time": [timestamp.isoformat() for timestamp in timestamps],
        "label_end_time": [timestamp.isoformat() for timestamp in label_end],
        "label_available_time": [timestamp.isoformat() for timestamp in label_end],
        "horizon_seconds": [horizon_seconds] * len(timestamps),
        "barrier_path_sha256": [digest] * len(timestamps),
        "barrier_path": {"path": str(barrier_path), "sha256": digest},
    }


def _feature_snapshot_row(name: str, value: float) -> dict[str, object]:
    row: dict[str, object] = {
        "name": name,
        "value": value,
        "event_time": (OPEN - timedelta(hours=1)).isoformat(),
        "published_time": (OPEN - timedelta(minutes=20)).isoformat(),
        "available_time": OPEN.isoformat(),
        "ingested_time": OPEN.isoformat(),
        "revision_time": None,
        "source": "test-pit-store",
        "source_record_id": f"source-{name}",
        "feature_registry_version": "features-v1",
    }
    encoded = json.dumps(
        row,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    row["content_hash"] = hashlib.sha256(encoded).hexdigest()
    return row


def _calibrated(
    tmp_path: Path,
    probability: float = 0.62,
    *,
    horizon: str = "1h",
    horizon_seconds: float = 3600.0,
) -> CalibratedProbabilityEvidence:
    dataset_path = tmp_path / "dataset.csv"
    dataset_path.write_text("timestamp,return_1h,spread\n", encoding="utf-8")
    dataset_hash = _sha256(dataset_path)
    feature_version = "features-v1"
    label_version = "triple-barrier-v1"
    selected_trial_id = "trial-1"
    target_definition = "tp_before_sl"
    direction = "long"
    entry_price = 155.0
    stop_price = 154.25
    target_price = 155.75
    cost_r = 0.0533
    raw_calibration = [0.9] * 120 + [0.1] * 120
    labels = [1] * 72 + [0] * 48 + [1] * 48 + [0] * 72
    fitted = fit_calibrator("platt", raw_calibration, labels)
    scale = fitted.scale
    offset = fitted.offset
    raw_prediction = 1.0 / (
        1.0
        + __import__("math").exp(
            -((__import__("math").log(probability / (1.0 - probability)) - offset) / scale)
        )
    )
    weights_path = tmp_path / "model.weights"
    weights_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "model_type": "logistic_regression",
                "feature_names": ["return_1h", "spread"],
                "coefficients": [0.0, 0.0],
                "intercept": __import__("math").log(raw_prediction / (1.0 - raw_prediction)),
            }
        ),
        encoding="utf-8",
    )
    model_path = tmp_path / "model-manifest.json"
    model_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "model_version": "model-v1",
                "dataset_hash": dataset_hash,
                "feature_version": feature_version,
                "label_version": label_version,
                "selected_trial_id": selected_trial_id,
                "symbol": "USDJPY",
                "horizon": horizon,
                "target_definition": target_definition,
                "direction": direction,
                "entry_price": entry_price,
                "stop_price": stop_price,
                "target_price": target_price,
                "cost_r": cost_r,
                "training_window_end": (OPEN - timedelta(days=8)).isoformat(),
                "model_type": "logistic_regression",
                "feature_names": ["return_1h", "spread"],
                "weights": {"path": str(weights_path), "sha256": _sha256(weights_path)},
            }
        ),
        encoding="utf-8",
    )
    feature_registry_path = tmp_path / "feature-registry.json"
    feature_registry_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "version": feature_version,
                "features": ["return_1h", "spread"],
            }
        ),
        encoding="utf-8",
    )
    feature_rows = [
        _feature_snapshot_row("return_1h", 0.001),
        _feature_snapshot_row("spread", 0.02),
    ]
    feature_store_path = tmp_path / "feature-store.json"
    feature_store_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "dataset_artifact": {
                    "path": str(dataset_path),
                    "sha256": dataset_hash,
                },
                "feature_registry": {
                    "path": str(feature_registry_path),
                    "sha256": _sha256(feature_registry_path),
                },
                "feature_registry_version": feature_version,
                "records": feature_rows,
            }
        ),
        encoding="utf-8",
    )
    input_path = tmp_path / "prediction-input.json"
    input_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "model_version": "model-v1",
                "dataset_hash": dataset_hash,
                "feature_version": feature_version,
                "label_version": label_version,
                "selected_trial_id": selected_trial_id,
                "symbol": "USDJPY",
                "horizon": horizon,
                "target_definition": target_definition,
                "direction": direction,
                "entry_price": entry_price,
                "stop_price": stop_price,
                "target_price": target_price,
                "cost_r": cost_r,
                "prediction_time": OPEN.isoformat(),
                "feature_store": {
                    "path": str(feature_store_path),
                    "sha256": _sha256(feature_store_path),
                },
                "feature_snapshot": feature_rows,
                "raw_probability": raw_prediction,
            }
        ),
        encoding="utf-8",
    )
    calibration_timestamps = [
        OPEN - timedelta(days=7) + timedelta(minutes=5 * index) for index in range(240)
    ]
    calibrated_values = [
        1.0
        / (
            1.0
            + __import__("math").exp(
                -(scale * __import__("math").log(value / (1.0 - value)) + offset)
            )
        )
        for value in raw_calibration
    ]
    barrier_path = tmp_path / "barrier-path.json"
    barrier_path.write_text(
        json.dumps({"schema_version": 1, "label_version": label_version}),
        encoding="utf-8",
    )
    calibration_metrics = _compute_calibration_metrics(
        labels,
        raw_calibration,
        calibrated_values,
    )
    observations_path = tmp_path / "calibration-observations.json"
    observations_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "model_version": "model-v1",
                "dataset_hash": dataset_hash,
                "feature_version": feature_version,
                "label_version": label_version,
                "selected_trial_id": selected_trial_id,
                "symbol": "USDJPY",
                "horizon": horizon,
                "target_definition": target_definition,
                "direction": direction,
                "entry_price": entry_price,
                "stop_price": stop_price,
                "target_price": target_price,
                "cost_r": cost_r,
                **_label_timing(
                    calibration_timestamps,
                    barrier_path,
                    horizon_seconds=horizon_seconds,
                ),
                "y_true": labels,
                "raw_probability": raw_calibration,
                "calibrated_probability": calibrated_values,
            }
        ),
        encoding="utf-8",
    )
    holdout_timestamps = [
        OPEN - timedelta(days=3) + timedelta(minutes=5 * index) for index in range(240)
    ]
    holdout_path = tmp_path / "calibration-holdout.json"
    holdout_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "model_version": "model-v1",
                "dataset_hash": dataset_hash,
                "feature_version": feature_version,
                "label_version": label_version,
                "selected_trial_id": selected_trial_id,
                "symbol": "USDJPY",
                "horizon": horizon,
                "target_definition": target_definition,
                "direction": direction,
                "entry_price": entry_price,
                "stop_price": stop_price,
                "target_price": target_price,
                "cost_r": cost_r,
                **_label_timing(
                    holdout_timestamps,
                    barrier_path,
                    horizon_seconds=horizon_seconds,
                ),
                "y_true": labels,
                "raw_probability": raw_calibration,
                "calibrated_probability": calibrated_values,
            }
        ),
        encoding="utf-8",
    )
    calibrator_path = tmp_path / "calibrator.json"
    calibrator_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "model_version": "model-v1",
                "dataset_hash": dataset_hash,
                "feature_version": feature_version,
                "label_version": label_version,
                "selected_trial_id": selected_trial_id,
                "symbol": "USDJPY",
                "horizon": horizon,
                "target_definition": target_definition,
                "direction": direction,
                "entry_price": entry_price,
                "stop_price": stop_price,
                "target_price": target_price,
                "cost_r": cost_r,
                "method": "platt",
                "windows": {
                    "calibration_start": (OPEN - timedelta(days=7)).isoformat(),
                    "calibration_end": (OPEN - timedelta(days=6)).isoformat(),
                    "selection_start": (OPEN - timedelta(days=5)).isoformat(),
                    "selection_end": (OPEN - timedelta(days=4)).isoformat(),
                },
                "method_selection": {
                    "schema_version": 1,
                    "candidate_methods": ["platt"],
                    "selected_method": "platt",
                    "selection_metric": "predeclared",
                    "predeclared_at": (OPEN - timedelta(days=8, hours=1)).isoformat(),
                },
                "metrics": calibration_metrics,
                "holdout_metrics": calibration_metrics,
                "calibration_observations": {
                    "path": str(observations_path),
                    "sha256": _sha256(observations_path),
                },
                "calibration_holdout": {
                    "path": str(holdout_path),
                    "sha256": _sha256(holdout_path),
                },
                "parameters": {"scale": scale, "offset": offset},
            }
        ),
        encoding="utf-8",
    )
    ledger_path = tmp_path / "trials.jsonl"
    ledger_path.write_text(
        "".join(
            json.dumps(
                {
                    "trial_id": f"trial-{index}",
                    "status": "complete",
                    "dataset_hash": dataset_hash,
                    "feature_version": feature_version,
                    "label_version": label_version,
                    "config_hash": f"{index}" * 64,
                    "model_artifact_hash": (_sha256(model_path) if index == 1 else f"{index}" * 64),
                    "started_at": (OPEN - timedelta(days=10, hours=index)).isoformat(),
                    "completed_at": (
                        OPEN - timedelta(days=10, hours=index) + timedelta(minutes=30)
                    ).isoformat(),
                }
            )
            + "\n"
            for index in (1, 2)
        ),
        encoding="utf-8",
    )
    evidence_path = tmp_path / "calibrated_prediction.json"
    evidence_path.write_text(
        json.dumps(
            {
                "schema_version": 4,
                "probability": probability,
                "model_version": "model-v1",
                "dataset_hash": dataset_hash,
                "feature_version": feature_version,
                "label_version": label_version,
                "selected_trial_id": selected_trial_id,
                "calibrator_method": "platt",
                "symbol": "USDJPY",
                "horizon": horizon,
                "target_definition": target_definition,
                "direction": direction,
                "entry_price": entry_price,
                "stop_price": stop_price,
                "target_price": target_price,
                "cost_r": cost_r,
                "prediction_time": OPEN.isoformat(),
                "windows": {
                    "training_end": (OPEN - timedelta(days=8)).isoformat(),
                    "calibration_start": (OPEN - timedelta(days=7)).isoformat(),
                    "calibration_end": (OPEN - timedelta(days=6)).isoformat(),
                    "selection_start": (OPEN - timedelta(days=5)).isoformat(),
                    "selection_end": (OPEN - timedelta(days=4)).isoformat(),
                    "test_start": (OPEN - timedelta(days=3)).isoformat(),
                    "test_end": (OPEN - timedelta(days=2)).isoformat(),
                },
                "model_artifact": {
                    "path": str(model_path),
                    "sha256": _sha256(model_path),
                },
                "calibrator_artifact": {
                    "path": str(calibrator_path),
                    "sha256": _sha256(calibrator_path),
                },
                "prediction_input": {
                    "path": str(input_path),
                    "sha256": _sha256(input_path),
                },
                "trial_ledger": {
                    "path": str(ledger_path),
                    "sha256": _sha256(ledger_path),
                },
            }
        ),
        encoding="utf-8",
    )
    return calibrated_probability_from_artifact(evidence_path)


def _bullish_tech(
    *,
    spread: float | None = 0.02,
    atr: float | None = 0.30,
    close: float = 155.0,
) -> PairTechnicals:
    """全時間足ロング目線・SL/TPを引ける健全なテクニカル。"""
    tech = PairTechnicals(symbol="USDJPY", fast_window=20, slow_window=100)
    bid = close - (spread / 2 if spread else 0.0)
    ask = close + (spread / 2 if spread else 0.0)
    tech.views["1h"] = IntervalView(
        interval="1h",
        recommendation="STRONG_BUY",
        buy=14,
        sell=1,
        neutral=2,
        close=close,
        bid=bid if spread else None,
        ask=ask if spread else None,
        spread=spread,
        rsi=58.0,
        atr=atr,
        sma_fast=close + 0.6,
        sma_slow=close - 0.2,
    )
    tech.views["4h"] = IntervalView(
        interval="4h",
        recommendation="BUY",
        buy=10,
        sell=1,
        neutral=2,
        close=close,
        sma_fast=close + 0.8,
        sma_slow=close - 0.3,
    )
    tech.views["1d"] = IntervalView(
        interval="1d",
        recommendation="BUY",
        buy=9,
        sell=1,
        neutral=3,
        close=close,
        sma_fast=close + 1.0,
        sma_slow=close - 0.5,
    )
    return tech


def _scores() -> dict[str, CurrencySentiment]:
    # USD強気・JPY弱気 → USDJPYロング寄り。tech と方向を揃える。
    return {
        "USD": CurrencySentiment("USD", score=0.4),
        "JPY": CurrencySentiment("JPY", score=-0.3),
    }


def _step(checklist, key):
    return next(s for s in checklist.steps if s.key == key)


# --- 純粋関数 ---------------------------------------------------------------


def test_estimate_expected_r_grows_with_conviction() -> None:
    low = estimate_expected_r("long", 20, target1_r=1.0)
    high = estimate_expected_r("long", 90, target1_r=1.0)
    assert high > low
    # neutral は常に0
    assert estimate_expected_r("neutral", 90, 1.0) == 0.0


def test_execution_cost_in_r() -> None:
    # SL距離0.75、スプレッド0.02、スリッページ1本 → コスト0.04価格 / 0.75 = 0.0533R
    cost = execution_cost_in_r(0.02, 0.75, slippage_spreads=1.0)
    assert cost is not None
    assert abs(cost - 0.0533) < 1e-3
    # 不明入力は None
    assert execution_cost_in_r(None, 0.75) is None
    assert execution_cost_in_r(0.02, 0.0) is None
    assert execution_cost_in_r(-0.02, 0.75) is None
    assert execution_cost_in_r(True, 0.75) is None
    assert execution_cost_in_r(0.02, True) is None
    assert execution_cost_in_r(0.02, 0.75, slippage_spreads=True) is None
    assert execution_cost_in_r(float("inf"), 0.75) is None
    assert execution_cost_in_r(10**10_000, 0.75) is None


def test_position_units() -> None:
    # USD口座: $5,000 risk / (0.75 JPY / 155 USDJPY) = 約1,033,333通貨。
    units = position_units(
        1_000_000,
        0.5,
        0.75,
        symbol="USDJPY",
        entry_price=155.0,
    )
    assert units is not None
    assert abs(units - 1_033_333.33) < 1.0
    assert position_units(None, 0.5, 0.75, symbol="USDJPY", entry_price=155.0) is None
    assert position_units(1_000_000, 0.5, 0.0, symbol="USDJPY", entry_price=155.0) is None
    assert position_units(1_000_000, 0.5, 0.75) is None
    assert position_units(True, 0.5, 0.75, symbol="USDJPY", entry_price=155.0) is None
    assert position_units(1_000_000, True, 0.75, symbol="USDJPY", entry_price=155.0) is None
    assert position_units(1e308, 1e308, 0.75, symbol="USDJPY", entry_price=155.0) is None
    assert position_units(10**10_000, 0.5, 0.75, symbol="USDJPY", entry_price=155.0) is None
    assert (
        position_units(
            100_000,
            0.5,
            0.01,
            symbol="EURGBP",
            entry_price=0.85,
            conversion_rates={"GBPUSD": -1.2},
        )
        is None
    )
    with_cost = position_units(
        1_000_000,
        0.5,
        0.75,
        symbol="USDJPY",
        entry_price=155.0,
        extra_risk_price=0.04,
    )
    assert with_cost is not None and with_cost < units


def test_calibrated_evidence_recomputes_hashes_and_requires_independent_windows(
    tmp_path: Path,
) -> None:
    evidence = _calibrated(tmp_path)
    bound = {
        "direction": "long",
        "entry_price": 155.0,
        "stop_price": 154.25,
        "target_price": 155.75,
        "cost_r": 0.0533,
    }
    assert evidence.valid_at(OPEN, "USDJPY", "1h", **bound)
    assert not evidence.valid_at(OPEN, "USDJPY", "24h", **bound)

    forged = replace(evidence, probability=0.99)
    assert not forged.valid_at(OPEN, "USDJPY", "1h", **bound)
    Path(evidence.model_artifact_path).write_bytes(b"tampered")
    assert not evidence.valid_at(OPEN, "USDJPY", "1h", **bound)

    overlap_dir = tmp_path / "overlap"
    overlap_dir.mkdir()
    good = _calibrated(overlap_dir)
    evidence_path = Path(good.evidence_path)
    payload = json.loads(evidence_path.read_text(encoding="utf-8"))
    payload["windows"]["test_start"] = payload["windows"]["selection_end"]
    evidence_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="overlap"):
        calibrated_probability_from_artifact(evidence_path)


def test_calibration_evidence_rejects_self_reported_probability_and_dummy_ledger(
    tmp_path: Path,
) -> None:
    model_path = tmp_path / "model.bin"
    model_path.write_bytes(b"not-a-model")
    ledger_path = tmp_path / "trials.txt"
    ledger_path.write_text("not-json-not-a-trial-ledger", encoding="utf-8")
    evidence_path = tmp_path / "forged.json"
    evidence_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "probability": 0.99,
                "model_version": "forged",
                "calibrator_method": "platt",
                "symbol": "USDJPY",
                "horizon": "1h",
                "prediction_time": OPEN.isoformat(),
                "model_artifact": {"path": str(model_path), "sha256": _sha256(model_path)},
                "trial_ledger": {"path": str(ledger_path), "sha256": _sha256(ledger_path)},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="schema_version must be 4"):
        calibrated_probability_from_artifact(evidence_path)


def test_calibration_evidence_recomputes_probability_from_bound_input(
    tmp_path: Path,
) -> None:
    evidence = _calibrated(tmp_path, probability=0.62)
    path = Path(evidence.evidence_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["probability"] = 0.99
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="does not match immutable calibrator output"):
        calibrated_probability_from_artifact(path)


# --- チェックリスト全体 -----------------------------------------------------


def test_unsealed_research_probability_is_explanatory_only(tmp_path: Path) -> None:
    plan, checklist = run_pipeline(
        "USDJPY",
        _bullish_tech(),
        _scores(),
        [],
        [],
        now=OPEN,
        horizon="1h",
        account_balance=1_000_000,
        calibrated_probability=_calibrated(tmp_path),
    )
    assert plan.direction == "long"
    # 9ステップが順番どおり
    assert [s.order for s in checklist.steps] == list(range(1, 10))
    assert checklist.blocked
    # 執行コスト控除後の純期待Rが正
    assert checklist.net_expected_r is not None and checklist.net_expected_r > 0
    # Unsealed research evidence stops before sizing.
    assert checklist.position_units is None
    assert checklist.probability_calibrated
    assert "research-only" in checklist.expectancy_source
    assert checklist.final_action == "no_trade"
    assert plan.direction == "long"
    assert plan.action == "no_trade"


def test_weekend_blocks_at_regime_step() -> None:
    plan, checklist = run_pipeline("USDJPY", _bullish_tech(), _scores(), [], [], now=CLOSED)
    assert plan.direction == "closed"
    assert _step(checklist, "regime").status == "block"
    assert checklist.blocked


def test_missing_atr_blocks_volatility_step() -> None:
    plan, checklist = run_pipeline("USDJPY", _bullish_tech(atr=None), _scores(), [], [], now=OPEN)
    assert _step(checklist, "volatility").status == "block"
    assert checklist.blocked


def test_huge_atr_warns_volatility() -> None:
    # close=155, atr=3.5 → 2.26% > ATR_PCT_MAX
    tech = _bullish_tech(atr=3.5)
    _, checklist = run_pipeline("USDJPY", tech, _scores(), [], [], now=OPEN)
    vol = _step(checklist, "volatility")
    assert vol.status == "warn"
    assert (3.5 / 155.0 * 100) > ATR_PCT_MAX


def test_wide_spread_blocks_at_spread_step() -> None:
    # SL距離 = atr*2.5 = 0.75。スプレッドをその25%超(=0.20)に。
    tech = _bullish_tech(spread=0.20, atr=0.30)
    _, checklist = run_pipeline("USDJPY", tech, _scores(), [], [], now=OPEN)
    spread_step = _step(checklist, "spread")
    assert spread_step.status == "block"
    assert (0.20 / 0.75) >= SPREAD_BLOCK_FRACTION
    assert checklist.blocked


def test_wide_spread_makes_net_expectancy_negative_or_blocks() -> None:
    # スプレッドが広いと執行コスト控除で純期待Rが削られる。
    tech = _bullish_tech(spread=0.20, atr=0.30)
    _, checklist = run_pipeline("USDJPY", tech, _scores(), [], [], now=OPEN)
    # 前段(spread)でblock済み。コストR自体も大きい。
    assert checklist.execution_cost_r is not None
    assert checklist.execution_cost_r > 0.2


def test_no_balance_blocks_sizing_instead_of_showing_unbound_risk(tmp_path: Path) -> None:
    plan, checklist = run_pipeline(
        "USDJPY",
        _bullish_tech(),
        _scores(),
        [],
        [],
        now=OPEN,
        horizon="1h",
        account_balance=None,
        calibrated_probability=_calibrated(tmp_path),
    )
    if plan.direction == "long":
        size_step = _step(checklist, "position_size")
        assert size_step.status == "skip"
        assert checklist.position_units is None
        assert checklist.final_action == "no_trade"
        assert plan.direction == "long"
        assert plan.action == "no_trade"


def test_realized_expectancy_overrides_theoretical() -> None:
    # 実測期待Rが非正なら期待値ステップでblock。
    _, checklist = run_pipeline(
        "USDJPY",
        _bullish_tech(),
        _scores(),
        [],
        [],
        now=OPEN,
        realized_expectancy_r=-0.3,
    )
    exp = _step(checklist, "expectancy")
    assert exp.status == "block"
    assert checklist.expected_r == -0.3


def test_positive_naked_realized_expectancy_cannot_self_certify_trade() -> None:
    plan, checklist = run_pipeline(
        "USDJPY",
        _bullish_tech(),
        _scores(),
        [],
        [],
        now=OPEN,
        account_balance=1_000_000,
        realized_expectancy_r=2.0,
    )

    assert _step(checklist, "expectancy").status == "block"
    assert checklist.final_action == "no_trade"
    assert plan.action == "no_trade"


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), float("-inf"), True])
def test_invalid_realized_expectancy_is_a_hard_no_trade(invalid: object) -> None:
    _, checklist = run_pipeline(
        "USDJPY",
        _bullish_tech(),
        _scores(),
        [],
        [],
        now=OPEN,
        account_balance=1_000_000,
        realized_expectancy_r=invalid,  # type: ignore[arg-type]
    )

    assert _step(checklist, "expectancy").status == "block"
    assert checklist.final_action == "no_trade"


def test_uncalibrated_conviction_cannot_pass_expectancy_gate() -> None:
    _, checklist = run_pipeline("USDJPY", _bullish_tech(), _scores(), [], [], now=OPEN)

    assert _step(checklist, "expectancy").status == "block"
    assert not checklist.probability_calibrated
    assert "未較正" in checklist.expectancy_source


def test_missing_spread_blocks_cost_gate_instead_of_assuming_zero_cost(tmp_path: Path) -> None:
    _, checklist = run_pipeline(
        "USDJPY",
        _bullish_tech(spread=None),
        _scores(),
        [],
        [],
        now=OPEN,
        horizon="1h",
        calibrated_probability=_calibrated(tmp_path),
    )

    assert _step(checklist, "execution_cost").status == "block"
    assert checklist.net_expected_r is None


def test_operational_freshness_veto_forces_neutral_and_blocks_checklist(tmp_path: Path) -> None:
    plan, checklist = run_pipeline(
        "USDJPY",
        _bullish_tech(),
        _scores(),
        [],
        [],
        now=OPEN,
        horizon="1h",
        calibrated_probability=_calibrated(tmp_path),
        operational_data_ok=False,
        operational_data_reason="freshness report stale",
    )

    assert plan.direction == "neutral"
    assert plan.conviction == 0
    assert _step(checklist, "event").status == "block"
    assert "freshness report stale" in _step(checklist, "event").note
    assert checklist.final_action == "no_trade"


def test_checklist_serialization_roundtrip() -> None:
    _, checklist = run_pipeline("USDJPY", _bullish_tech(), _scores(), [], [], now=OPEN)
    data = checklist.to_dict()
    assert data["symbol"] == "USDJPY"
    assert data["final_action"] == "no_trade"
    assert len(data["steps"]) == 9
    assert all({"order", "key", "status"} <= set(step) for step in data["steps"])


def test_huge_slippage_fails_closed_without_overflow(tmp_path: Path) -> None:
    plan, checklist = run_pipeline(
        "USDJPY",
        _bullish_tech(),
        _scores(),
        [],
        [],
        now=OPEN,
        horizon="1h",
        account_balance=1_000_000,
        slippage_spreads=10**10000,
        calibrated_probability=_calibrated(tmp_path),
    )

    assert _step(checklist, "execution_cost").status == "block"
    assert checklist.execution_cost_r is None
    assert checklist.position_units is None
    assert checklist.final_action == "no_trade"
    assert plan.action == "no_trade"


def test_run_pipeline_propagates_horizon_to_plan_and_checklist() -> None:
    plan, checklist = run_pipeline(
        "USDJPY",
        _bullish_tech(),
        _scores(),
        [],
        [],
        now=OPEN,
        horizon="4h",
    )

    assert plan.horizon_hours == 4.0
    assert checklist.horizon == "4h"
    assert checklist.horizon_hours == 4.0
    assert plan.checklist["horizon"] == "4h"
    assert plan.checklist["horizon_hours"] == 4.0


def test_calibration_for_different_horizon_is_explicitly_blocked(tmp_path: Path) -> None:
    plan, checklist = run_pipeline(
        "USDJPY",
        _bullish_tech(),
        _scores(),
        [],
        [],
        now=OPEN,
        calibrated_probability=_calibrated(tmp_path),
    )

    assert plan.horizon_hours == 24.0
    assert checklist.horizon == "24h"
    assert _step(checklist, "expectancy").status == "block"
    assert "plan=24h, calibration=1h" in checklist.expectancy_source
    assert checklist.final_action == "no_trade"


def test_calibration_rejects_feature_projection_not_used_by_model(tmp_path: Path) -> None:
    evidence = _calibrated(tmp_path)
    evidence_path = Path(evidence.evidence_path)
    payload = json.loads(evidence_path.read_text(encoding="utf-8"))
    input_path = Path(payload["prediction_input"]["path"])
    prediction_input = json.loads(input_path.read_text(encoding="utf-8"))
    prediction_input["features"] = {"attacker_feature": 999.0}
    input_path.write_text(json.dumps(prediction_input), encoding="utf-8")
    payload["prediction_input"]["sha256"] = _sha256(input_path)
    evidence_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="closed point-in-time schema"):
        calibrated_probability_from_artifact(evidence_path)


def test_calibration_rejects_self_reported_metrics_without_matching_observations(
    tmp_path: Path,
) -> None:
    evidence = _calibrated(tmp_path)
    evidence_path = Path(evidence.evidence_path)
    payload = json.loads(evidence_path.read_text(encoding="utf-8"))
    calibrator_path = Path(payload["calibrator_artifact"]["path"])
    calibrator = json.loads(calibrator_path.read_text(encoding="utf-8"))
    calibrator["metrics"]["calibrated_brier"] = 0.0001
    calibrator_path.write_text(json.dumps(calibrator), encoding="utf-8")
    payload["calibrator_artifact"]["sha256"] = _sha256(calibrator_path)
    evidence_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="immutable calibration observations"):
        calibrated_probability_from_artifact(evidence_path)


def test_calibration_rejects_parameters_not_refitted_from_calibration_window(
    tmp_path: Path,
) -> None:
    evidence = _calibrated(tmp_path)
    evidence_path = Path(evidence.evidence_path)
    payload = json.loads(evidence_path.read_text(encoding="utf-8"))
    calibrator_path = Path(payload["calibrator_artifact"]["path"])
    calibrator = json.loads(calibrator_path.read_text(encoding="utf-8"))
    calibrator["parameters"]["scale"] = 0.5
    observations_path = Path(calibrator["calibration_observations"]["path"])
    holdout_path = Path(calibrator["calibration_holdout"]["path"])
    for observations_file in (observations_path, holdout_path):
        observations = json.loads(observations_file.read_text(encoding="utf-8"))
        observations["calibrated_probability"] = [
            1.0 / (1.0 + __import__("math").exp(-0.5 * __import__("math").log(value / (1 - value))))
            for value in observations["raw_probability"]
        ]
        observations_file.write_text(json.dumps(observations), encoding="utf-8")
    calibrator["calibration_observations"]["sha256"] = _sha256(observations_path)
    calibrator["calibration_holdout"]["sha256"] = _sha256(holdout_path)
    calibration = json.loads(observations_path.read_text(encoding="utf-8"))
    holdout = json.loads(holdout_path.read_text(encoding="utf-8"))
    calibrator["metrics"] = _compute_calibration_metrics(
        calibration["y_true"],
        calibration["raw_probability"],
        calibration["calibrated_probability"],
    )
    calibrator["holdout_metrics"] = _compute_calibration_metrics(
        holdout["y_true"],
        holdout["raw_probability"],
        holdout["calibrated_probability"],
    )
    calibrator_path.write_text(json.dumps(calibrator), encoding="utf-8")
    payload["calibrator_artifact"]["sha256"] = _sha256(calibrator_path)
    evidence_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="deterministic calibration-window fit"):
        calibrated_probability_from_artifact(evidence_path)


def test_calibration_rejects_72h_labels_without_required_purge(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not purged before the next window"):
        _calibrated(tmp_path, horizon="72h", horizon_seconds=72 * 3600.0)


def test_calibration_rejects_label_available_after_next_window_boundary(
    tmp_path: Path,
) -> None:
    evidence = _calibrated(tmp_path)
    evidence_path = Path(evidence.evidence_path)
    payload = json.loads(evidence_path.read_text(encoding="utf-8"))
    calibrator_path = Path(payload["calibrator_artifact"]["path"])
    calibrator = json.loads(calibrator_path.read_text(encoding="utf-8"))
    observations_path = Path(calibrator["calibration_observations"]["path"])
    observations = json.loads(observations_path.read_text(encoding="utf-8"))
    selection_start = datetime.fromisoformat(payload["windows"]["selection_start"])
    observations["label_available_time"][-1] = (
        selection_start + timedelta(microseconds=1)
    ).isoformat()
    observations_path.write_text(json.dumps(observations), encoding="utf-8")
    calibrator["calibration_observations"]["sha256"] = _sha256(observations_path)
    calibrator_path.write_text(json.dumps(calibrator), encoding="utf-8")
    payload["calibrator_artifact"]["sha256"] = _sha256(calibrator_path)
    evidence_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="unavailable before the next window"):
        calibrated_probability_from_artifact(evidence_path)


def test_calibration_rejects_rehashed_fake_barrier_path_binding(tmp_path: Path) -> None:
    evidence = _calibrated(tmp_path)
    evidence_path = Path(evidence.evidence_path)
    payload = json.loads(evidence_path.read_text(encoding="utf-8"))
    calibrator_path = Path(payload["calibrator_artifact"]["path"])
    calibrator = json.loads(calibrator_path.read_text(encoding="utf-8"))
    observations_path = Path(calibrator["calibration_observations"]["path"])
    observations = json.loads(observations_path.read_text(encoding="utf-8"))
    observations["barrier_path_sha256"][0] = "b" * 64
    observations_path.write_text(json.dumps(observations), encoding="utf-8")
    calibrator["calibration_observations"]["sha256"] = _sha256(observations_path)
    calibrator_path.write_text(json.dumps(calibrator), encoding="utf-8")
    payload["calibrator_artifact"]["sha256"] = _sha256(calibrator_path)
    evidence_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="bind the barrier-path artifact"):
        calibrated_probability_from_artifact(evidence_path)


def test_multiple_calibrator_candidates_require_an_unimplemented_selection_artifact(
    tmp_path: Path,
) -> None:
    evidence = _calibrated(tmp_path)
    evidence_path = Path(evidence.evidence_path)
    payload = json.loads(evidence_path.read_text(encoding="utf-8"))
    calibrator_path = Path(payload["calibrator_artifact"]["path"])
    calibrator = json.loads(calibrator_path.read_text(encoding="utf-8"))
    calibrator["method_selection"]["candidate_methods"] = ["platt", "isotonic"]
    calibrator_path.write_text(json.dumps(calibrator), encoding="utf-8")
    payload["calibrator_artifact"]["sha256"] = _sha256(calibrator_path)
    evidence_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="only one predeclared calibrator"):
        calibrated_probability_from_artifact(evidence_path)


def test_prediction_feature_must_exist_in_bound_pit_store(tmp_path: Path) -> None:
    evidence = _calibrated(tmp_path)
    evidence_path = Path(evidence.evidence_path)
    payload = json.loads(evidence_path.read_text(encoding="utf-8"))
    input_path = Path(payload["prediction_input"]["path"])
    prediction_input = json.loads(input_path.read_text(encoding="utf-8"))
    row = prediction_input["feature_snapshot"][0]
    row["value"] = 999.0
    hash_payload = {key: value for key, value in row.items() if key != "content_hash"}
    row["content_hash"] = hashlib.sha256(
        json.dumps(
            hash_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    input_path.write_text(json.dumps(prediction_input), encoding="utf-8")
    payload["prediction_input"]["sha256"] = _sha256(input_path)
    evidence_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="immutable source record"):
        calibrated_probability_from_artifact(evidence_path)


def test_research_evidence_cannot_be_locally_marked_authorized(tmp_path: Path) -> None:
    forged = replace(_calibrated(tmp_path), decision_authorized=True)

    plan, checklist = run_pipeline(
        "USDJPY",
        _bullish_tech(),
        _scores(),
        [],
        [],
        now=OPEN,
        horizon="1h",
        account_balance=1_000_000,
        calibrated_probability=forged,
    )

    assert checklist.final_action == "no_trade"
    assert plan.action == "no_trade"
    assert not checklist.probability_calibrated


def test_long_probability_evidence_cannot_authorize_short_plan(tmp_path: Path) -> None:
    tech = _bullish_tech()
    tech.views = {
        key: replace(
            view,
            recommendation="STRONG_SELL",
            buy=1,
            sell=14,
            sma_fast=(view.close or 155.0) - 1.0,
            sma_slow=(view.close or 155.0) + 1.0,
        )
        for key, view in tech.views.items()
    }
    scores = {
        "USD": CurrencySentiment("USD", score=-0.4),
        "JPY": CurrencySentiment("JPY", score=0.3),
    }

    plan, checklist = run_pipeline(
        "USDJPY",
        tech,
        scores,
        [],
        [],
        now=OPEN,
        horizon="1h",
        account_balance=1_000_000,
        calibrated_probability=_calibrated(tmp_path, probability=0.90),
    )

    assert plan.direction == "short"
    assert checklist.final_action == "no_trade"
    assert not checklist.probability_calibrated


def test_calendar_unavailable_is_an_explicit_hard_block(tmp_path: Path) -> None:
    plan, checklist = run_pipeline(
        "USDJPY",
        _bullish_tech(),
        _scores(),
        [],
        [],
        now=OPEN,
        horizon="1h",
        account_balance=1_000_000,
        calibrated_probability=_calibrated(tmp_path),
        calendar_ok=False,
    )

    assert _step(checklist, "event").status == "block"
    assert checklist.final_action == "no_trade"
    assert plan.action == "no_trade"


def test_calibration_rejects_future_feature_availability_even_when_rehashed(
    tmp_path: Path,
) -> None:
    evidence = _calibrated(tmp_path)
    evidence_path = Path(evidence.evidence_path)
    payload = json.loads(evidence_path.read_text(encoding="utf-8"))
    input_path = Path(payload["prediction_input"]["path"])
    prediction_input = json.loads(input_path.read_text(encoding="utf-8"))
    row = prediction_input["feature_snapshot"][0]
    row["available_time"] = (OPEN + timedelta(hours=1)).isoformat()
    hash_payload = {key: value for key, value in row.items() if key != "content_hash"}
    row["content_hash"] = hashlib.sha256(
        json.dumps(
            hash_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    input_path.write_text(json.dumps(prediction_input), encoding="utf-8")
    payload["prediction_input"]["sha256"] = _sha256(input_path)
    evidence_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="unavailable at prediction_time"):
        calibrated_probability_from_artifact(evidence_path)


def test_calibration_requires_improvement_on_independent_holdout(tmp_path: Path) -> None:
    evidence = _calibrated(tmp_path)
    evidence_path = Path(evidence.evidence_path)
    payload = json.loads(evidence_path.read_text(encoding="utf-8"))
    calibrator_path = Path(payload["calibrator_artifact"]["path"])
    calibrator = json.loads(calibrator_path.read_text(encoding="utf-8"))
    holdout_path = Path(calibrator["calibration_holdout"]["path"])
    holdout = json.loads(holdout_path.read_text(encoding="utf-8"))
    labels = [1] * 114 + [0] * 6 + [1] * 6 + [0] * 114
    holdout["y_true"] = labels
    holdout_path.write_text(json.dumps(holdout), encoding="utf-8")
    calibrator["calibration_holdout"]["sha256"] = _sha256(holdout_path)
    calibrator["holdout_metrics"] = _compute_calibration_metrics(
        labels,
        holdout["raw_probability"],
        holdout["calibrated_probability"],
    )
    calibrator_path.write_text(json.dumps(calibrator), encoding="utf-8")
    payload["calibrator_artifact"]["sha256"] = _sha256(calibrator_path)
    evidence_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="independent calibrator holdout"):
        calibrated_probability_from_artifact(evidence_path)


def test_selected_trial_must_bind_verified_model_artifact(tmp_path: Path) -> None:
    evidence = _calibrated(tmp_path)
    evidence_path = Path(evidence.evidence_path)
    payload = json.loads(evidence_path.read_text(encoding="utf-8"))
    ledger_path = Path(payload["trial_ledger"]["path"])
    rows = [json.loads(line) for line in ledger_path.read_text(encoding="utf-8").splitlines()]
    rows[0]["model_artifact_hash"] = "0" * 64
    ledger_path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    payload["trial_ledger"]["sha256"] = _sha256(ledger_path)
    evidence_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="selected trial does not bind"):
        calibrated_probability_from_artifact(evidence_path)
