"""9段チェックリスト型の意思決定パイプライン。

ユーザーが求めた「機関投資家デスクの発注前チェックリスト」を、順序どおりの
明示的なゲート列として実装する。各ステップは CheckStep として結果(ok/warn/
block)と理由を残すため、なぜその判断になったかを1判断まるごと監査できる。

    1. MAクロス            — 自作MAクロス戦略の目線があるか
    2. 市場レジーム判定     — リスクオン/オフ等のレジームと方向の整合
    3. 上位足との整合       — 上位足(4h/1d)がエントリー方向と揃っているか
    4. ボラティリティ確認   — ATRが過小/過大でないか(SL/TP算出可能か)
    5. 流動性・スプレッド確認 — スプレッドがSL距離に対して許容内か
    6. ニュース・金利・イベント確認 — 高影響イベント窓・カレンダー欠損
    7. 期待値計算           — TP/SL込みの期待R(確信度と勝率から素の期待値)
    8. 執行コスト控除       — スプレッド+スリッページを期待Rから差し引く
    9. ポジションサイズ決定 — 口座リスク%とSL距離からロットを算出

このモジュールは build_trade_plan(=リスクオフィサーの決定論ゲート)の
上位互換ラッパー。build_trade_plan が既に計算している値(方向・確信度・
SL/TP・イベント窓・品質)を順序付きチェックリストに写像し、コードに未実装
だった 5(スプレッド)/8(執行コスト)/9(サイズ)の3ステップを足す。

追加のサードパーティ依存は無し(標準ライブラリのみ。Mac miniの軽量venvに
そのまま移設できる — analyst/macro/gbm 等と同じ方針)。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, UTC
from collections.abc import Callable, Mapping, Sequence
import hashlib
import json
from math import exp, isfinite, log
from numbers import Real
from pathlib import Path
from typing import cast, TypeGuard

from fx_backtester.models import (
    UnsupportedConversionError,
    price_distance_to_usd_per_unit,
)
from fx_backtester.calibration import CalibrationError, CalibrationMethod, fit_calibrator

from .briefing import (
    DEFAULT_ATR_MULTIPLE,
    DEFAULT_RISK_PCT,
    DEFAULT_TARGET1_R,
    ScoreComponent,
    TargetRAdjuster,
    TradePlan,
    build_trade_plan,
)
from .market import is_market_open
from .technicals import PairTechnicals

# --- 実行コスト/流動性のしきい値 ---------------------------------------------
# スプレッドがSL距離のこの割合を超えたら「流動性が薄い/コスト過大」で警告。
# さらに BLOCK 側の閾値を超えたら新規エントリーを見送る。
SPREAD_WARN_FRACTION = 0.10  # SL距離の10%
SPREAD_BLOCK_FRACTION = 0.25  # SL距離の25%(これ以上はエッジをコストが食い潰す)

# 想定スリッページ。約定は次足始値・成行前提なので、スプレッドに上乗せする。
# スプレッド1本ぶんを既定のスリッページ見積りとする(保守的)。
DEFAULT_SLIPPAGE_SPREADS = 1.0

# ボラティリティ(ATR%)の許容レンジ。過小はダマシ/コスト負け、過大は
# ストップ幅が広がりすぎてサイズが取れない。方向判断は変えず警告に留める。
ATR_PCT_MIN = 0.02  # 0.02%未満は動意薄
ATR_PCT_MAX = 1.50  # 1.5%超は異常なボラ

# 期待値の素点(勝率×TP - 敗率×SL)を確信度から見積もる際の勝率換算。
# conviction=100 で勝率 WIN_RATE_AT_FULL、conviction=0 で 0.5(五分)。
WIN_RATE_AT_FULL = 0.62

STATUS_EMOJI = {"ok": "✅", "warn": "⚠️", "block": "⛔", "skip": "➖"}


@dataclass
class CheckStep:
    """チェックリスト1ステップの結果。"""

    order: int
    key: str
    label_ja: str
    status: str  # "ok" / "warn" / "block" / "skip"
    note: str = ""

    @property
    def emoji(self) -> str:
        return STATUS_EMOJI.get(self.status, "•")

    def line_ja(self) -> str:
        body = f"{self.emoji} {self.order}. {self.label_ja}"
        return f"{body} — {self.note}" if self.note else body

    def to_dict(self) -> dict[str, object]:
        return {
            "order": self.order,
            "key": self.key,
            "label_ja": self.label_ja,
            "status": self.status,
            "note": self.note,
        }


@dataclass(frozen=True)
class CalibratedProbabilityEvidence:
    """A calibrated prediction bound to immutable, independently checked artifacts."""

    probability: float
    model_version: str
    dataset_hash: str
    feature_version: str
    label_version: str
    selected_trial_id: str
    calibrator_method: str
    symbol: str
    horizon: str
    target_definition: str
    direction: str
    entry_price: float
    stop_price: float
    target_price: float
    cost_r: float
    prediction_time: datetime
    training_window_end: datetime
    calibration_window_start: datetime
    calibration_window_end: datetime
    selection_window_start: datetime
    selection_window_end: datetime
    test_window_start: datetime
    test_window_end: datetime
    evidence_path: str
    artifact_hash: str
    model_artifact_path: str
    model_artifact_hash: str
    calibrator_artifact_path: str
    calibrator_artifact_hash: str
    calibration_holdout_path: str
    calibration_holdout_hash: str
    prediction_input_path: str
    prediction_input_hash: str
    trial_ledger_path: str
    trial_ledger_hash: str
    decision_authorized: bool = False

    def valid_at(
        self,
        prediction_time: datetime,
        symbol: str,
        horizon: str,
        *,
        direction: str,
        entry_price: float,
        stop_price: float,
        target_price: float,
        cost_r: float,
    ) -> bool:
        if prediction_time.tzinfo is None or self.prediction_time.tzinfo is None:
            return False
        if self.prediction_time.astimezone(UTC) != prediction_time.astimezone(UTC):
            return False
        if self.symbol != symbol or self.horizon != horizon:
            return False
        if self.target_definition != "tp_before_sl" or self.direction != direction:
            return False
        observed = (entry_price, stop_price, target_price, cost_r)
        expected = (self.entry_price, self.stop_price, self.target_price, self.cost_r)
        if any(
            not _finite_real(value)
            or not isfinite(reference)
            or abs(float(value) - reference) > 1e-12
            for value, reference in zip(observed, expected, strict=True)
        ):
            return False
        return _calibration_evidence_still_matches(self)


def calibrated_probability_from_artifact(
    evidence_path: str | Path,
) -> CalibratedProbabilityEvidence:
    """Load a calibrated prediction only after verifying its immutable provenance.

    The evidence file binds the scalar prediction to a versioned model manifest,
    calibrator, prediction input, complete trial ledger, symbol/horizon, and
    strictly ordered train/calibration/selection/independent-test windows.  The
    calibrated probability is recomputed from the raw probability and immutable
    calibrator parameters; a caller-provided scalar can therefore not self-certify.
    """

    path = Path(evidence_path).resolve()
    payload = _read_json_object(path, "calibration evidence")
    if payload.get("schema_version") != 4:
        raise ValueError("calibration evidence schema_version must be 4")
    probability = _required_finite_number(payload, "probability")
    if not 0.0 <= probability <= 1.0:
        raise ValueError("calibration evidence probability must be in [0, 1]")
    model_version = _required_text(payload, "model_version")
    dataset_hash = _required_sha256(payload, "dataset_hash")
    feature_version = _required_text(payload, "feature_version")
    label_version = _required_text(payload, "label_version")
    selected_trial_id = _required_text(payload, "selected_trial_id")
    method = _required_text(payload, "calibrator_method")
    if method not in {"platt", "isotonic", "beta"}:
        raise ValueError("calibration evidence has an unsupported calibrator_method")
    symbol = _required_text(payload, "symbol")
    horizon = _required_text(payload, "horizon")
    target_definition = _required_text(payload, "target_definition")
    if target_definition != "tp_before_sl":
        raise ValueError("calibration evidence target_definition must be tp_before_sl")
    direction = _required_text(payload, "direction")
    if direction not in {"long", "short"}:
        raise ValueError("calibration evidence direction must be long or short")
    entry_price = _required_positive_number(payload, "entry_price")
    stop_price = _required_positive_number(payload, "stop_price")
    target_price = _required_positive_number(payload, "target_price")
    cost_r = _required_finite_number(payload, "cost_r")
    if cost_r < 0.0:
        raise ValueError("calibration evidence cost_r must be non-negative")
    if direction == "long" and not stop_price < entry_price < target_price:
        raise ValueError("long calibration barriers must satisfy stop < entry < target")
    if direction == "short" and not target_price < entry_price < stop_price:
        raise ValueError("short calibration barriers must satisfy target < entry < stop")
    prediction_time = _required_aware_time(payload, "prediction_time")
    windows = payload.get("windows")
    if not isinstance(windows, Mapping):
        raise ValueError("calibration evidence windows must be an object")
    training_end = _required_aware_time(windows, "training_end")
    calibration_start = _required_aware_time(windows, "calibration_start")
    calibration_end = _required_aware_time(windows, "calibration_end")
    selection_start = _required_aware_time(windows, "selection_start")
    selection_end = _required_aware_time(windows, "selection_end")
    test_start = _required_aware_time(windows, "test_start")
    test_end = _required_aware_time(windows, "test_end")
    ordered = (
        training_end,
        calibration_start,
        calibration_end,
        selection_start,
        selection_end,
        test_start,
        test_end,
        prediction_time,
    )
    if any(left >= right for left, right in zip(ordered, ordered[1:])):
        raise ValueError("calibration evidence windows overlap or are not strictly ordered")

    model_path, model_hash = _verified_file_reference(payload, "model_artifact", path.parent)
    calibrator_path, calibrator_hash = _verified_file_reference(
        payload, "calibrator_artifact", path.parent
    )
    input_path, input_hash = _verified_file_reference(payload, "prediction_input", path.parent)
    ledger_path, ledger_hash = _verified_file_reference(payload, "trial_ledger", path.parent)
    binding = {
        "model_version": model_version,
        "dataset_hash": dataset_hash,
        "feature_version": feature_version,
        "label_version": label_version,
        "selected_trial_id": selected_trial_id,
        "symbol": symbol,
        "horizon": horizon,
        "target_definition": target_definition,
        "direction": direction,
        "entry_price": entry_price,
        "stop_price": stop_price,
        "target_price": target_price,
        "cost_r": cost_r,
    }
    feature_names = _verify_model_manifest(model_path, binding, training_end)
    raw_probability = _verify_prediction_input(
        input_path,
        binding,
        prediction_time,
        model_path,
        feature_names,
    )
    calibrated, holdout_path, holdout_hash = _verify_calibrator_artifact(
        calibrator_path,
        binding,
        method,
        calibration_start,
        calibration_end,
        selection_start,
        selection_end,
        test_start,
        test_end,
        prediction_time,
        raw_probability,
    )
    if abs(calibrated - probability) > 1e-12:
        raise ValueError("calibrated probability does not match immutable calibrator output")
    _verify_calibration_trial_ledger(
        ledger_path,
        binding,
        model_artifact_hash=model_hash,
        selection_end=selection_end,
        test_start=test_start,
    )
    evidence_hash = _sha256_file(path)
    return CalibratedProbabilityEvidence(
        probability=probability,
        model_version=model_version,
        dataset_hash=dataset_hash,
        feature_version=feature_version,
        label_version=label_version,
        selected_trial_id=selected_trial_id,
        calibrator_method=method,
        symbol=symbol,
        horizon=horizon,
        target_definition=target_definition,
        direction=direction,
        entry_price=entry_price,
        stop_price=stop_price,
        target_price=target_price,
        cost_r=cost_r,
        prediction_time=prediction_time,
        training_window_end=training_end,
        calibration_window_start=calibration_start,
        calibration_window_end=calibration_end,
        selection_window_start=selection_start,
        selection_window_end=selection_end,
        test_window_start=test_start,
        test_window_end=test_end,
        evidence_path=str(path),
        artifact_hash=evidence_hash,
        model_artifact_path=str(model_path),
        model_artifact_hash=model_hash,
        calibrator_artifact_path=str(calibrator_path),
        calibrator_artifact_hash=calibrator_hash,
        calibration_holdout_path=str(holdout_path),
        calibration_holdout_hash=holdout_hash,
        prediction_input_path=str(input_path),
        prediction_input_hash=input_hash,
        trial_ledger_path=str(ledger_path),
        trial_ledger_hash=ledger_hash,
    )


def _verify_model_manifest(
    path: Path,
    binding: Mapping[str, object],
    training_end: datetime,
) -> tuple[str, ...]:
    manifest = _read_json_object(path, "model artifact manifest")
    if manifest.get("schema_version") != 1:
        raise ValueError("model artifact manifest schema_version must be 1")
    _require_binding(manifest, binding, "model artifact manifest")
    if _required_aware_time(manifest, "training_window_end") != training_end:
        raise ValueError("model artifact training window does not match evidence")
    features = manifest.get("feature_names")
    if (
        not isinstance(features, list)
        or not features
        or not all(isinstance(value, str) and value.strip() for value in features)
    ):
        raise ValueError("model artifact feature_names must be a non-empty string list")
    model_type = _required_text(manifest, "model_type")
    if model_type != "logistic_regression":
        raise ValueError("only logistic_regression model artifacts can be verified")
    weights_path, _ = _verified_file_reference(manifest, "weights", path.parent)
    weights = _read_json_object(weights_path, "model weights")
    if weights.get("schema_version") != 1 or weights.get("model_type") != model_type:
        raise ValueError("model weights schema/model_type mismatch")
    if weights.get("feature_names") != features:
        raise ValueError("model weights feature_names do not match model manifest")
    coefficients = weights.get("coefficients")
    if not isinstance(coefficients, list) or len(coefficients) != len(features):
        raise ValueError("model weights coefficients must match feature_names")
    for position, value in enumerate(coefficients):
        _finite_list_value(value, f"model coefficient[{position}]")
    _required_finite_number(weights, "intercept")
    return tuple(str(value) for value in features)


def _verify_prediction_input(
    path: Path,
    binding: Mapping[str, object],
    prediction_time: datetime,
    model_path: Path,
    feature_names: tuple[str, ...],
) -> float:
    prediction_input = _read_json_object(path, "prediction input")
    if prediction_input.get("schema_version") != 2:
        raise ValueError("prediction input schema_version must be 2")
    expected_keys = {
        "schema_version",
        *binding.keys(),
        "prediction_time",
        "feature_store",
        "feature_snapshot",
        "raw_probability",
    }
    if set(prediction_input) != expected_keys:
        raise ValueError("prediction input must use the closed point-in-time schema")
    _require_binding(prediction_input, binding, "prediction input")
    if _required_aware_time(prediction_input, "prediction_time") != prediction_time:
        raise ValueError("prediction input time does not match evidence")
    feature_snapshot = prediction_input.get("feature_snapshot")
    if not isinstance(feature_snapshot, list) or not feature_snapshot:
        raise ValueError("prediction input feature_snapshot must be a non-empty list")
    snapshot_names = tuple(
        row.get("name") if isinstance(row, Mapping) else None for row in feature_snapshot
    )
    if snapshot_names != feature_names:
        raise ValueError("point-in-time feature snapshot must exactly match model feature order")
    store_path, _ = _verified_file_reference(prediction_input, "feature_store", path.parent)
    store_records = _verify_feature_store(
        store_path,
        binding,
        feature_names=feature_names,
    )
    parsed_features: list[float] = []
    for position, row in enumerate(feature_snapshot):
        if not isinstance(row, Mapping) or set(row) != {
            "name",
            "value",
            "event_time",
            "published_time",
            "available_time",
            "ingested_time",
            "revision_time",
            "source",
            "source_record_id",
            "feature_registry_version",
            "content_hash",
        }:
            raise ValueError("point-in-time feature row uses an invalid closed schema")
        name = _required_text(row, "name")
        value = row.get("value")
        if not isinstance(value, Real) or isinstance(value, bool) or not isfinite(float(value)):
            raise ValueError("prediction input feature values must be finite numbers")
        event_time = _required_aware_time(row, "event_time")
        published_time = _required_aware_time(row, "published_time")
        available_time = _required_aware_time(row, "available_time")
        ingested_time = _required_aware_time(row, "ingested_time")
        revision_time = _optional_aware_time(row, "revision_time")
        effective_availability = max(
            published_time,
            available_time,
            ingested_time,
            revision_time or available_time,
        )
        if event_time > effective_availability or effective_availability > prediction_time:
            raise ValueError(f"feature {name} was unavailable at prediction_time")
        source = _required_text(row, "source")
        source_record_id = _required_text(row, "source_record_id")
        if row.get("feature_registry_version") != binding["feature_version"]:
            raise ValueError("feature snapshot registry version does not match evidence")
        content_hash = _required_sha256(row, "content_hash")
        hash_payload = {key: value for key, value in row.items() if key != "content_hash"}
        encoded = json.dumps(
            hash_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        if hashlib.sha256(encoded).hexdigest() != content_hash:
            raise ValueError(f"feature snapshot content hash mismatch at position {position}")
        store_key = (source, source_record_id, name)
        if store_records.get(store_key) != dict(row):
            raise ValueError(f"feature {name} does not match its immutable source record")
        parsed_features.append(float(value))
    model_manifest = _read_json_object(model_path, "model artifact manifest")
    weights_path, _ = _verified_file_reference(model_manifest, "weights", model_path.parent)
    weights = _read_json_object(weights_path, "model weights")
    coefficients = weights.get("coefficients")
    if not isinstance(coefficients, list) or len(coefficients) != len(parsed_features):
        raise ValueError("model coefficient count does not match prediction input")
    parsed_coefficients = [
        _finite_list_value(value, f"model coefficient[{position}]")
        for position, value in enumerate(coefficients)
    ]
    intercept = _required_finite_number(weights, "intercept")
    margin = intercept + sum(
        coefficient * feature
        for coefficient, feature in zip(parsed_coefficients, parsed_features, strict=True)
    )
    if not isfinite(margin):
        raise ValueError("model margin must be finite")
    computed = 1.0 / (1.0 + exp(-max(-35.0, min(35.0, margin))))
    recorded = _required_finite_number(prediction_input, "raw_probability")
    if abs(recorded - computed) > 1e-12:
        raise ValueError("prediction input raw_probability does not match model inference")
    return computed


def _verify_feature_store(
    path: Path,
    binding: Mapping[str, object],
    *,
    feature_names: tuple[str, ...],
) -> dict[tuple[str, str, str], dict[str, object]]:
    store = _read_json_object(path, "point-in-time feature store")
    if (
        set(store)
        != {
            "schema_version",
            "dataset_artifact",
            "feature_registry",
            "feature_registry_version",
            "records",
        }
        or store.get("schema_version") != 1
    ):
        raise ValueError("point-in-time feature store must use the closed schema")
    dataset_path, dataset_hash = _verified_file_reference(
        store,
        "dataset_artifact",
        path.parent,
    )
    if dataset_hash != binding["dataset_hash"] or not dataset_path.is_file():
        raise ValueError("feature store dataset artifact does not match calibration evidence")
    registry_path, registry_hash = _verified_file_reference(
        store,
        "feature_registry",
        path.parent,
    )
    if store.get("feature_registry_version") != binding["feature_version"]:
        raise ValueError("feature store registry version does not match calibration evidence")
    registry = _read_json_object(registry_path, "feature registry")
    if (
        set(registry) != {"schema_version", "version", "features"}
        or registry.get("schema_version") != 1
    ):
        raise ValueError("feature registry must use the closed schema")
    if registry.get("version") != binding["feature_version"] or registry.get("features") != list(
        feature_names
    ):
        raise ValueError("feature registry does not match the selected model features")
    # The verified reference is deliberately consumed even though the registry
    # payload is also parsed; this makes the registry hash part of the evidence.
    if not _is_sha256(registry_hash):
        raise ValueError("feature registry hash is invalid")
    records = store.get("records")
    if not isinstance(records, list) or not records:
        raise ValueError("point-in-time feature store requires source records")
    verified: dict[tuple[str, str, str], dict[str, object]] = {}
    for position, row in enumerate(records):
        if not isinstance(row, Mapping) or set(row) != {
            "name",
            "value",
            "event_time",
            "published_time",
            "available_time",
            "ingested_time",
            "revision_time",
            "source",
            "source_record_id",
            "feature_registry_version",
            "content_hash",
        }:
            raise ValueError("feature store record uses an invalid closed schema")
        name = _required_text(row, "name")
        source = _required_text(row, "source")
        source_record_id = _required_text(row, "source_record_id")
        if row.get("feature_registry_version") != binding["feature_version"]:
            raise ValueError("feature store record registry version mismatch")
        value = row.get("value")
        if not isinstance(value, Real) or isinstance(value, bool) or not isfinite(float(value)):
            raise ValueError("feature store values must be finite numbers")
        for key in ("event_time", "published_time", "available_time", "ingested_time"):
            _required_aware_time(row, key)
        _optional_aware_time(row, "revision_time")
        content_hash = _required_sha256(row, "content_hash")
        hash_payload = {key: value for key, value in row.items() if key != "content_hash"}
        encoded = json.dumps(
            hash_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        if hashlib.sha256(encoded).hexdigest() != content_hash:
            raise ValueError(f"feature store content hash mismatch at position {position}")
        record_key = (source, source_record_id, name)
        if record_key in verified:
            raise ValueError("feature store source record keys must be unique")
        verified[record_key] = dict(row)
    return verified


def _verify_calibrator_artifact(
    path: Path,
    binding: Mapping[str, object],
    method: str,
    calibration_start: datetime,
    calibration_end: datetime,
    selection_start: datetime,
    selection_end: datetime,
    test_start: datetime,
    test_end: datetime,
    prediction_time: datetime,
    raw_probability: float,
) -> tuple[float, Path, str]:
    calibrator = _read_json_object(path, "calibrator artifact")
    if calibrator.get("schema_version") != 1:
        raise ValueError("calibrator artifact schema_version must be 1")
    _require_binding(calibrator, binding, "calibrator artifact")
    if calibrator.get("method") != method:
        raise ValueError("calibrator artifact method does not match evidence")
    windows = calibrator.get("windows")
    if not isinstance(windows, Mapping):
        raise ValueError("calibrator artifact windows must be an object")
    expected_windows = {
        "calibration_start": calibration_start,
        "calibration_end": calibration_end,
        "selection_start": selection_start,
        "selection_end": selection_end,
    }
    for key, expected in expected_windows.items():
        if _required_aware_time(windows, key) != expected:
            raise ValueError(f"calibrator artifact {key} does not match evidence")
    metrics = calibrator.get("metrics")
    if not isinstance(metrics, Mapping):
        raise ValueError("calibrator artifact metrics must be an object")
    parameters = calibrator.get("parameters")
    if not isinstance(parameters, Mapping):
        raise ValueError("calibrator artifact parameters must be an object")
    observations_path, _ = _verified_file_reference(
        calibrator,
        "calibration_observations",
        path.parent,
    )
    recomputed, fit_labels, fit_raw = _verify_calibration_observations(
        observations_path,
        binding,
        method,
        parameters,
        calibration_start,
        calibration_end,
        availability_cutoff=selection_start,
    )
    if metrics != recomputed:
        raise ValueError("calibrator metrics do not match immutable calibration observations")
    _verify_predeclared_calibration_method(
        calibrator,
        method,
        calibration_start=calibration_start,
        selection_start=selection_start,
        test_start=test_start,
    )
    expected_parameters = _refit_calibrator_parameters(method, fit_raw, fit_labels)
    if not _calibrator_parameters_match(method, parameters, expected_parameters):
        raise ValueError("calibrator parameters do not match deterministic calibration-window fit")
    holdout_metrics = calibrator.get("holdout_metrics")
    if not isinstance(holdout_metrics, Mapping):
        raise ValueError("calibrator artifact holdout_metrics must be an object")
    holdout_path, holdout_hash = _verified_file_reference(
        calibrator,
        "calibration_holdout",
        path.parent,
    )
    recomputed_holdout, _, _ = _verify_calibration_observations(
        holdout_path,
        binding,
        method,
        parameters,
        test_start,
        test_end,
        availability_cutoff=prediction_time,
    )
    if holdout_metrics != recomputed_holdout:
        raise ValueError("calibrator holdout metrics do not match immutable observations")
    if not (
        recomputed_holdout["calibrated_brier"] < recomputed_holdout["raw_brier"]
        and recomputed_holdout["calibrated_log_loss"] < recomputed_holdout["raw_log_loss"]
        and recomputed_holdout["calibration_slope"] > 0.0
    ):
        raise ValueError("independent calibrator holdout does not demonstrate improvement")
    return (
        _apply_immutable_calibrator(method, parameters, raw_probability),
        holdout_path,
        holdout_hash,
    )


def _apply_immutable_calibrator(
    method: str,
    parameters: Mapping[str, object],
    raw_probability: float,
) -> float:
    if method == "platt":
        scale = _required_finite_number(parameters, "scale")
        offset = _required_finite_number(parameters, "offset")
        if scale <= 0.0:
            raise ValueError("platt scale must be positive")
        margin = scale * log(raw_probability / (1.0 - raw_probability)) + offset
        return 1.0 / (1.0 + exp(-max(-35.0, min(35.0, margin))))
    if method == "beta":
        log_p_scale = _required_finite_number(parameters, "log_p_scale")
        log_one_minus_p_scale = _required_finite_number(parameters, "log_one_minus_p_scale")
        offset = _required_finite_number(parameters, "offset")
        if log_p_scale <= 0.0 or log_one_minus_p_scale <= 0.0:
            raise ValueError("beta calibration scales must be positive")
        margin = (
            log_p_scale * log(raw_probability)
            - log_one_minus_p_scale * log(1.0 - raw_probability)
            + offset
        )
        return 1.0 / (1.0 + exp(-max(-35.0, min(35.0, margin))))
    upper_bounds = parameters.get("upper_bounds")
    values = parameters.get("values")
    if not isinstance(upper_bounds, list) or not isinstance(values, list):
        raise ValueError("isotonic parameters require upper_bounds and values")
    if not upper_bounds or len(upper_bounds) != len(values):
        raise ValueError("isotonic parameter arrays must be non-empty and equal length")
    parsed_bounds = [_finite_list_value(value, "isotonic upper bound") for value in upper_bounds]
    parsed_values = [_finite_list_value(value, "isotonic value") for value in values]
    if parsed_bounds != sorted(parsed_bounds) or any(
        not 0.0 <= value <= 1.0 for value in (*parsed_bounds, *parsed_values)
    ):
        raise ValueError("isotonic parameters must be monotone probabilities")
    for bound, value in zip(parsed_bounds, parsed_values, strict=True):
        if raw_probability <= bound:
            return value
    return parsed_values[-1]


def _verify_calibration_observations(
    path: Path,
    binding: Mapping[str, object],
    method: str,
    parameters: Mapping[str, object],
    calibration_start: datetime,
    calibration_end: datetime,
    *,
    availability_cutoff: datetime,
) -> tuple[dict[str, int | float], list[int], list[float]]:
    payload = _read_json_object(path, "calibration observations")
    if payload.get("schema_version") != 1:
        raise ValueError("calibration observations schema_version must be 1")
    expected_keys = {
        "schema_version",
        *binding.keys(),
        "prediction_time",
        "label_end_time",
        "label_available_time",
        "horizon_seconds",
        "barrier_path_sha256",
        "barrier_path",
        "y_true",
        "raw_probability",
        "calibrated_probability",
    }
    if set(payload) != expected_keys:
        raise ValueError("calibration observations must use the closed label-evidence schema")
    _require_binding(payload, binding, "calibration observations")
    raw_timestamps = payload.get("prediction_time")
    labels = payload.get("y_true")
    raw_probabilities = payload.get("raw_probability")
    calibrated_probabilities = payload.get("calibrated_probability")
    if not all(
        isinstance(value, list)
        for value in (raw_timestamps, labels, raw_probabilities, calibrated_probabilities)
    ):
        raise ValueError("calibration observations require four observation arrays")
    assert isinstance(raw_timestamps, list)
    assert isinstance(labels, list)
    assert isinstance(raw_probabilities, list)
    assert isinstance(calibrated_probabilities, list)
    rows = len(raw_timestamps)
    if rows < 200 or any(
        len(value) != rows for value in (labels, raw_probabilities, calibrated_probabilities)
    ):
        raise ValueError("calibration observations require at least 200 aligned rows")
    timestamps = [
        _required_aware_time({"timestamp": value}, "timestamp") for value in raw_timestamps
    ]
    if any(
        timestamp < calibration_start or timestamp > calibration_end for timestamp in timestamps
    ):
        raise ValueError("calibration observations fall outside the calibration window")
    if any(left >= right for left, right in zip(timestamps, timestamps[1:])):
        raise ValueError("calibration observation timestamps must be unique and ordered")
    _verify_calibration_label_timing(
        payload,
        timestamps,
        binding=binding,
        availability_cutoff=availability_cutoff,
        base_directory=path.parent,
    )
    parsed_labels: list[int] = []
    parsed_raw: list[float] = []
    parsed_calibrated: list[float] = []
    for position, (label, raw, calibrated) in enumerate(
        zip(labels, raw_probabilities, calibrated_probabilities, strict=True)
    ):
        if not isinstance(label, int) or isinstance(label, bool) or label not in (0, 1):
            raise ValueError(f"calibration y_true[{position}] must be binary integer")
        raw_value = _finite_list_value(raw, f"calibration raw_probability[{position}]")
        calibrated_value = _finite_list_value(
            calibrated,
            f"calibration calibrated_probability[{position}]",
        )
        if not 0.0 < raw_value < 1.0 or not 0.0 < calibrated_value < 1.0:
            raise ValueError("calibration probabilities must be strictly inside (0, 1)")
        expected = _apply_immutable_calibrator(method, parameters, raw_value)
        if abs(expected - calibrated_value) > 1e-12:
            raise ValueError("calibrated observation does not match immutable calibrator")
        parsed_labels.append(label)
        parsed_raw.append(raw_value)
        parsed_calibrated.append(calibrated_value)
    return (
        _compute_calibration_metrics(parsed_labels, parsed_raw, parsed_calibrated),
        parsed_labels,
        parsed_raw,
    )


def _verify_calibration_label_timing(
    payload: Mapping[str, object],
    prediction_times: list[datetime],
    *,
    binding: Mapping[str, object],
    availability_cutoff: datetime,
    base_directory: Path,
) -> None:
    raw_end = payload.get("label_end_time")
    raw_available = payload.get("label_available_time")
    raw_horizon = payload.get("horizon_seconds")
    raw_barrier_hashes = payload.get("barrier_path_sha256")
    arrays = (raw_end, raw_available, raw_horizon, raw_barrier_hashes)
    if not all(isinstance(value, list) for value in arrays):
        raise ValueError("calibration labels require aligned timing and barrier arrays")
    assert isinstance(raw_end, list)
    assert isinstance(raw_available, list)
    assert isinstance(raw_horizon, list)
    assert isinstance(raw_barrier_hashes, list)
    aligned_arrays = (raw_end, raw_available, raw_horizon, raw_barrier_hashes)
    if any(len(value) != len(prediction_times) for value in aligned_arrays):
        raise ValueError("calibration label evidence arrays must be aligned")

    _, barrier_hash = _verified_file_reference(payload, "barrier_path", base_directory)
    if any(value != barrier_hash for value in raw_barrier_hashes):
        raise ValueError("every calibration label must bind the barrier-path artifact")
    expected_seconds = _horizon_hours_from_label(str(binding["horizon"])) * 3600.0
    for position, (prediction, raw_label_end, raw_label_available, raw_seconds) in enumerate(
        zip(prediction_times, raw_end, raw_available, raw_horizon, strict=True)
    ):
        label_end = _required_aware_time({"value": raw_label_end}, "value")
        label_available = _required_aware_time({"value": raw_label_available}, "value")
        if not isinstance(raw_seconds, Real) or isinstance(raw_seconds, bool):
            raise ValueError("calibration horizon_seconds must be numeric")
        seconds = float(raw_seconds)
        if not isfinite(seconds) or abs(seconds - expected_seconds) > 1e-9:
            raise ValueError("calibration horizon_seconds does not match evidence horizon")
        horizon_end = prediction + timedelta(seconds=seconds)
        if not prediction < label_end <= horizon_end:
            raise ValueError("calibration label end must be within its declared horizon")
        if horizon_end > availability_cutoff:
            raise ValueError("calibration label horizon is not purged before the next window")
        if not label_end <= label_available <= availability_cutoff:
            raise ValueError("calibration label was unavailable before the next window")


def _verify_predeclared_calibration_method(
    calibrator: Mapping[str, object],
    method: str,
    *,
    calibration_start: datetime,
    selection_start: datetime,
    test_start: datetime,
) -> None:
    policy = calibrator.get("method_selection")
    if not isinstance(policy, Mapping) or set(policy) != {
        "schema_version",
        "candidate_methods",
        "selected_method",
        "selection_metric",
        "predeclared_at",
    }:
        raise ValueError("calibrator method_selection must use the closed schema")
    candidates = policy.get("candidate_methods")
    if candidates != [method] or policy.get("selected_method") != method:
        raise ValueError("only one predeclared calibrator method is currently admissible")
    if policy.get("schema_version") != 1 or policy.get("selection_metric") != "predeclared":
        raise ValueError("calibrator method must be explicitly predeclared")
    predeclared_at = _required_aware_time(policy, "predeclared_at")
    if not predeclared_at < calibration_start < selection_start < test_start:
        raise ValueError("calibrator method was not frozen before calibration and test")


def _refit_calibrator_parameters(
    method: str,
    raw_probabilities: Sequence[float],
    labels: Sequence[int],
) -> dict[str, object]:
    try:
        fitted = fit_calibrator(cast(CalibrationMethod, method), raw_probabilities, labels)
    except CalibrationError as error:
        raise ValueError(
            "calibration-window observations cannot reproduce the calibrator"
        ) from error
    payload = fitted.to_dict()
    keys = {
        "platt": ("scale", "offset"),
        "beta": ("log_p_scale", "log_one_minus_p_scale", "offset"),
        "isotonic": ("upper_bounds", "values"),
    }[method]
    return {key: payload[key] for key in keys}


def _calibrator_parameters_match(
    method: str,
    actual: Mapping[str, object],
    expected: Mapping[str, object],
) -> bool:
    if set(actual) != set(expected):
        return False
    for key, reference in expected.items():
        observed = actual[key]
        if method in {"platt", "beta"}:
            if (
                not isinstance(observed, Real)
                or isinstance(observed, bool)
                or not isinstance(reference, Real)
                or isinstance(reference, bool)
                or not isfinite(float(observed))
                or abs(float(observed) - float(reference)) > 1e-12
            ):
                return False
            continue
        if not isinstance(observed, list) or not isinstance(reference, list):
            return False
        if len(observed) != len(reference):
            return False
        for observed_item, reference_item in zip(observed, reference, strict=True):
            if (
                not isinstance(observed_item, Real)
                or isinstance(observed_item, bool)
                or not isinstance(reference_item, Real)
                or isinstance(reference_item, bool)
                or not isfinite(float(observed_item))
                or abs(float(observed_item) - float(reference_item)) > 1e-12
            ):
                return False
    return True


def _compute_calibration_metrics(
    labels: Sequence[int],
    raw_probabilities: Sequence[float],
    calibrated_probabilities: Sequence[float],
) -> dict[str, int | float]:
    rows = len(labels)
    if rows == 0 or len(raw_probabilities) != rows or len(calibrated_probabilities) != rows:
        raise ValueError("calibration metric inputs must be non-empty and aligned")

    def brier(probabilities: Sequence[float]) -> float:
        return (
            sum((probability - label) ** 2 for probability, label in zip(probabilities, labels))
            / rows
        )

    def log_loss(probabilities: Sequence[float]) -> float:
        epsilon = 1e-15
        total = 0.0
        for probability, label in zip(probabilities, labels):
            clipped = min(1.0 - epsilon, max(epsilon, probability))
            total -= label * log(clipped) + (1 - label) * log(1.0 - clipped)
        return total / rows

    ece = 0.0
    for bucket in range(10):
        lower = bucket / 10.0
        upper = (bucket + 1) / 10.0
        positions = [
            position
            for position, probability in enumerate(calibrated_probabilities)
            if lower <= probability < upper or bucket == 9 and probability == 1.0
        ]
        if not positions:
            continue
        mean_probability = sum(calibrated_probabilities[position] for position in positions) / len(
            positions
        )
        mean_label = sum(labels[position] for position in positions) / len(positions)
        ece += len(positions) / rows * abs(mean_probability - mean_label)
    intercept, slope = _calibration_intercept_slope(labels, calibrated_probabilities)
    return {
        "sample_count": rows,
        "raw_brier": brier(raw_probabilities),
        "calibrated_brier": brier(calibrated_probabilities),
        "raw_log_loss": log_loss(raw_probabilities),
        "calibrated_log_loss": log_loss(calibrated_probabilities),
        "expected_calibration_error": ece,
        "calibration_slope": slope,
        "calibration_intercept": intercept,
    }


def _calibration_intercept_slope(
    labels: Sequence[int],
    probabilities: Sequence[float],
) -> tuple[float, float]:
    logits = [log(probability / (1.0 - probability)) for probability in probabilities]
    intercept = 0.0
    slope = 1.0
    for _ in range(100):
        fitted = [
            1.0 / (1.0 + exp(-max(-35.0, min(35.0, intercept + slope * value)))) for value in logits
        ]
        weights = [probability * (1.0 - probability) for probability in fitted]
        gradient_intercept = sum(label - probability for label, probability in zip(labels, fitted))
        gradient_slope = sum(
            (label - probability) * value
            for label, probability, value in zip(labels, fitted, logits)
        )
        info_00 = sum(weights)
        info_01 = sum(weight * value for weight, value in zip(weights, logits))
        info_11 = sum(weight * value * value for weight, value in zip(weights, logits))
        determinant = info_00 * info_11 - info_01 * info_01
        if determinant <= 1e-15 or not isfinite(determinant):
            raise ValueError("calibration slope/intercept are not identifiable")
        delta_intercept = (info_11 * gradient_intercept - info_01 * gradient_slope) / determinant
        delta_slope = (-info_01 * gradient_intercept + info_00 * gradient_slope) / determinant
        intercept += delta_intercept
        slope += delta_slope
        if not isfinite(intercept) or not isfinite(slope):
            raise ValueError("calibration slope/intercept must remain finite")
        if max(abs(delta_intercept), abs(delta_slope)) < 1e-12:
            return intercept, slope
    raise ValueError("calibration slope/intercept fit did not converge")


def _finite_list_value(value: object, label: str) -> float:
    if not isinstance(value, Real) or isinstance(value, bool) or not isfinite(float(value)):
        raise ValueError(f"{label} must be finite")
    return float(value)


def _verify_calibration_trial_ledger(
    path: Path,
    binding: Mapping[str, object],
    *,
    model_artifact_hash: str,
    selection_end: datetime,
    test_start: datetime,
) -> None:
    selected_seen = False
    trial_ids: set[str] = set()
    rows = 0
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw_line.strip():
            raise ValueError(f"blank calibration trial row at line {line_number}")
        try:
            row = json.loads(raw_line)
        except ValueError as error:
            raise ValueError(f"invalid calibration trial row at line {line_number}") from error
        if not isinstance(row, Mapping):
            raise ValueError("calibration trial rows must be objects")
        trial_id = _required_text(row, "trial_id")
        if trial_id in trial_ids:
            raise ValueError("calibration trial IDs must be unique")
        trial_ids.add(trial_id)
        if row.get("status") != "complete":
            raise ValueError("calibration trial ledger must contain only complete trials")
        for key in ("dataset_hash", "feature_version", "label_version"):
            if row.get(key) != binding[key]:
                raise ValueError(f"calibration trial {key} does not match evidence")
        _required_sha256(row, "config_hash")
        recorded_model_hash = _required_sha256(row, "model_artifact_hash")
        if trial_id == binding["selected_trial_id"] and recorded_model_hash != model_artifact_hash:
            raise ValueError("selected trial does not bind the verified model artifact")
        started = _required_aware_time(row, "started_at")
        completed = _required_aware_time(row, "completed_at")
        if started >= completed or completed > selection_end or completed >= test_start:
            raise ValueError("calibration trial timestamps are not ordered")
        selected_seen = selected_seen or trial_id == binding["selected_trial_id"]
        rows += 1
    if rows < 2:
        raise ValueError("calibration trial ledger requires at least two recorded trials")
    if not selected_seen:
        raise ValueError("selected calibration trial is absent from the trial ledger")


def _require_binding(
    payload: Mapping[str, object],
    binding: Mapping[str, object],
    label: str,
) -> None:
    for key, expected in binding.items():
        if payload.get(key) != expected:
            raise ValueError(f"{label} {key} does not match calibration evidence")


def _calibration_evidence_still_matches(evidence: CalibratedProbabilityEvidence) -> bool:
    try:
        loaded = calibrated_probability_from_artifact(evidence.evidence_path)
    except (OSError, ValueError, TypeError):
        return False
    return loaded == evidence


def _verified_file_reference(
    payload: Mapping[str, object],
    key: str,
    base_directory: Path,
) -> tuple[Path, str]:
    value = payload.get(key)
    if not isinstance(value, Mapping):
        raise ValueError(f"calibration evidence {key} must be an object")
    raw_path = value.get("path")
    digest = value.get("sha256")
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ValueError(f"calibration evidence {key}.path is required")
    if not _is_sha256(digest):
        raise ValueError(f"calibration evidence {key}.sha256 is invalid")
    path = Path(raw_path)
    if not path.is_absolute():
        path = base_directory / path
    path = path.resolve()
    if not path.is_file():
        raise ValueError(f"calibration evidence {key} is not a regular file")
    actual = _sha256_file(path)
    if actual != digest:
        raise ValueError(f"calibration evidence {key} SHA-256 mismatch")
    return path, actual


def _read_json_object(path: Path, label: str) -> dict[str, object]:
    if not path.is_file():
        raise ValueError(f"{label} is not a regular file")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError(f"{label} could not be read") from error
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _required_text(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value.strip()


def _required_sha256(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not _is_sha256(value):
        raise ValueError(f"{key} must be a lowercase SHA-256")
    return str(value)


def _required_finite_number(payload: Mapping[str, object], key: str) -> float:
    value = payload.get(key)
    if not isinstance(value, Real) or isinstance(value, bool) or not isfinite(float(value)):
        raise ValueError(f"{key} must be a finite number")
    return float(value)


def _required_positive_number(payload: Mapping[str, object], key: str) -> float:
    value = _required_finite_number(payload, key)
    if value <= 0.0:
        raise ValueError(f"{key} must be positive")
    return value


def _required_aware_time(payload: Mapping[str, object], key: str) -> datetime:
    value = payload.get(key)
    if not isinstance(value, str):
        raise ValueError(f"{key} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{key} must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None:
        raise ValueError(f"{key} must be timezone-aware")
    return parsed.astimezone(UTC)


def _optional_aware_time(payload: Mapping[str, object], key: str) -> datetime | None:
    value = payload.get(key)
    if value is None:
        return None
    return _required_aware_time(payload, key)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_sha256(value: object) -> bool:
    return bool(
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


@dataclass
class DecisionChecklist:
    """9ステップぶんの結果と最終判断のまとめ。"""

    symbol: str
    requested_direction: str = ""
    horizon: str = "24h"
    horizon_hours: float | None = 24.0
    steps: list[CheckStep] = field(default_factory=list)
    # 8以降で確定する実務値
    expected_r: float | None = None  # 執行コスト控除前の素の期待R
    net_expected_r: float | None = None  # スプレッド+スリッページ控除後
    execution_cost_r: float | None = None  # 控除したコスト(R換算)
    position_units: float | None = None  # ポジションサイズ(通貨単位/ロット)
    expectancy_source: str = ""
    probability_calibrated: bool = False

    @property
    def blocked(self) -> bool:
        return any(step.status == "block" for step in self.steps)

    @property
    def passed(self) -> bool:
        """全ステップが ok(見送り系ステップの block が無い)か。"""
        return not self.blocked

    @property
    def final_action(self) -> str:
        if self.blocked or self.requested_direction not in {"long", "short"}:
            return "no_trade"
        return self.requested_direction

    def summary_ja(self) -> str:
        return "\n".join(step.line_ja() for step in self.steps)

    def to_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "requested_direction": self.requested_direction,
            "horizon": self.horizon,
            "horizon_hours": self.horizon_hours,
            "final_action": self.final_action,
            "blocked": self.blocked,
            "expected_r": self.expected_r,
            "net_expected_r": self.net_expected_r,
            "execution_cost_r": self.execution_cost_r,
            "position_units": self.position_units,
            "expectancy_source": self.expectancy_source,
            "probability_calibrated": self.probability_calibrated,
            "steps": [step.to_dict() for step in self.steps],
        }


# --- 個別ステップの純粋関数(テストしやすいよう外だし) -----------------------


def _spread_at(tech: PairTechnicals, interval: str = "1h") -> float | None:
    """指定時間足のスプレッド(価格差)。無ければ None。"""
    view = tech.views.get(interval)
    if view is None:
        return None
    return view.spread


def estimate_expected_r(direction: str, conviction: int, target1_r: float) -> float:
    """確信度から素の期待R(執行コスト控除前)を見積もる。

    勝率 p = 0.5 + (conviction/100) * (WIN_RATE_AT_FULL - 0.5)
    期待R = p * target1_r - (1 - p) * 1.0   (負ければ -1R=SLに触れる想定)

    確信度が高いほど勝率が上がり、TPが遠い(target1_r大)ほど1勝の重みが増す。
    あくまで方向的中を含んだ素の理論値で、実測の期待R(maximization.py)が
    あればそちらが優先される(この関数はフォールバックの説明用途)。
    """
    if direction not in ("long", "short"):
        return 0.0
    p = 0.5 + (conviction / 100.0) * (WIN_RATE_AT_FULL - 0.5)
    p = max(0.0, min(1.0, p))
    return round(p * target1_r - (1.0 - p) * 1.0, 3)


def execution_cost_in_r(
    spread: float | None,
    stop_distance: float | None,
    slippage_spreads: float = DEFAULT_SLIPPAGE_SPREADS,
) -> float | None:
    """スプレッド+スリッページをR(=SL距離)換算で返す。

    1トレードのコスト = スプレッド * (1 + slippage_spreads)  [価格]
    R換算 = コスト / SL距離
    SL距離やスプレッドが不明なら None。
    """
    if (
        not _finite_real(spread)
        or not _finite_real(stop_distance)
        or spread <= 0
        or stop_distance <= 0
        or not _finite_real(slippage_spreads)
        or slippage_spreads < 0
    ):
        return None
    cost_price = spread * (1.0 + max(0.0, slippage_spreads))
    if not _finite_real(cost_price):
        return None
    return round(cost_price / stop_distance, 4)


def position_units(
    account_balance: float | None,
    risk_pct: float,
    stop_distance: float | None,
    *,
    symbol: str | None = None,
    entry_price: float | None = None,
    conversion_rates: dict[str, float] | None = None,
    extra_risk_price: float = 0.0,
) -> float | None:
    """Return USD-account FX units using quote-currency conversion.

    Missing symbol/price/conversion evidence returns ``None`` rather than showing a
    dimensionally wrong size. ``extra_risk_price`` includes estimated round-trip
    spread/slippage in quote-price units.
    """
    if (
        not _finite_real(account_balance)
        or account_balance <= 0
        or not _finite_real(risk_pct)
        or risk_pct <= 0
        or risk_pct > 100
    ):
        return None
    if (
        not _finite_real(stop_distance)
        or stop_distance <= 0
        or not symbol
        or not _finite_real(entry_price)
        or entry_price <= 0
        or not _finite_real(extra_risk_price)
        or extra_risk_price < 0
    ):
        return None
    if conversion_rates is not None and any(
        not isinstance(key, str) or not key.strip() or not _finite_real(value) or value <= 0
        for key, value in conversion_rates.items()
    ):
        return None
    risk_amount = account_balance * (risk_pct / 100.0)
    if not isfinite(risk_amount) or risk_amount <= 0:
        return None
    try:
        risk_per_unit = price_distance_to_usd_per_unit(
            symbol,
            stop_distance + extra_risk_price,
            entry_price,
            conversion_rates,
        )
    except (UnsupportedConversionError, ValueError, ZeroDivisionError, OverflowError):
        return None
    if not isfinite(risk_per_unit) or risk_per_unit <= 0:
        return None
    units = risk_amount / risk_per_unit
    if not isfinite(units) or units <= 0:
        return None
    return round(units, 2)


def _finite_real(value: object) -> TypeGuard[int | float]:
    if not isinstance(value, int | float) or isinstance(value, bool):
        return False
    try:
        return isfinite(value)
    except OverflowError:
        return False


def _horizon_hours_from_label(label: str) -> float:
    normalized = label.strip().lower()
    if normalized.endswith("m"):
        raw = normalized[:-1]
        divisor = 60.0
    elif normalized.endswith("h"):
        raw = normalized[:-1]
        divisor = 1.0
    else:
        raise ValueError("horizon must end in 'm' or 'h'")
    try:
        hours = float(raw) / divisor
    except ValueError as error:
        raise ValueError("horizon must contain a numeric duration") from error
    if not isfinite(hours) or hours <= 0:
        raise ValueError("horizon must be a finite positive duration")
    return hours


def _canonical_horizon(hours: float) -> str:
    standard = {0.25: "15m", 1.0: "1h", 4.0: "4h", 24.0: "24h", 72.0: "72h"}
    for candidate, label in standard.items():
        if abs(hours - candidate) < 1e-12:
            return label
    return f"{hours:g}h"


def build_checklist(
    plan: TradePlan,
    tech: PairTechnicals,
    *,
    now: datetime | None = None,
    account_balance: float | None = None,
    slippage_spreads: float = DEFAULT_SLIPPAGE_SPREADS,
    realized_expectancy_r: float | None = None,
    calibrated_probability: CalibratedProbabilityEvidence | None = None,
    horizon: str | None = None,
    calendar_ok: bool = True,
    operational_data_ok: bool = True,
    operational_data_reason: str = "",
) -> DecisionChecklist:
    """完成した TradePlan を 9ステップのチェックリストへ写像する。

    plan は build_trade_plan(=リスクオフィサー)が既に決めた最終判断。ここでは
    「どのステップがなぜそう判定されたか」を順序どおりに開示し、コードに未実装
    だったスプレッド/執行コスト/サイズの3ステップを追記する。

    realized_expectancy_r があれば期待値ステップにその実測値を使う
    (maximization.py の実測期待R)。無ければ確信度からの素点を使う。
    """
    now = now or datetime.now(UTC)
    plan_horizon_hours = (
        float(plan.horizon_hours)
        if _finite_real(plan.horizon_hours) and plan.horizon_hours > 0
        else None
    )
    plan_horizon = (
        _canonical_horizon(plan_horizon_hours) if plan_horizon_hours is not None else "invalid"
    )
    requested_horizon = horizon or plan_horizon
    try:
        requested_horizon_hours = _horizon_hours_from_label(requested_horizon)
    except ValueError:
        requested_horizon_hours = None
    horizon_mismatch = (
        plan_horizon_hours is None
        or requested_horizon_hours is None
        or abs(plan_horizon_hours - requested_horizon_hours) >= 1e-12
    )
    resolved_horizon = (
        _canonical_horizon(requested_horizon_hours)
        if requested_horizon_hours is not None
        else requested_horizon
    )
    calibration_horizon_mismatch = (
        calibrated_probability is not None and calibrated_probability.horizon != resolved_horizon
    )
    checklist = DecisionChecklist(
        symbol=plan.symbol,
        requested_direction=plan.direction,
        horizon=resolved_horizon,
        horizon_hours=plan_horizon_hours,
    )
    steps = checklist.steps
    directional = plan.direction in ("long", "short")

    # 1. MAクロス -------------------------------------------------------------
    ma_side = tech.ma_side("1h")
    if ma_side is None:
        steps.append(
            CheckStep(1, "ma_cross", "MAクロス", "warn", "MAクロスの目線を確定できず(MA未取得)")
        )
    else:
        steps.append(
            CheckStep(
                1,
                "ma_cross",
                "MAクロス",
                "ok",
                f"1h MAクロスは{_side_ja(ma_side)}目線 ({plan.ma_note})",
            )
        )

    # 2. 市場レジーム判定 -----------------------------------------------------
    if not is_market_open(now):
        steps.append(
            CheckStep(2, "regime", "市場レジーム判定", "block", "FX市場休場中(週末クローズ)")
        )
    else:
        steps.append(
            CheckStep(
                2,
                "regime",
                "市場レジーム判定",
                "ok",
                "市場オープン中。レジーム整合は委員会スコアに反映済み",
            )
        )

    # 3. 上位足との整合 -------------------------------------------------------
    agreement = tech.agreement_ratio()
    higher_side = tech.ma_side("4h") or tech.ma_side("1d")
    if agreement is None:
        steps.append(
            CheckStep(
                3,
                "htf_alignment",
                "上位足との整合",
                "warn",
                "上位足の向きを判定できず(全時間足中立/未取得)",
            )
        )
    elif directional and higher_side is not None and higher_side != plan.direction:
        steps.append(
            CheckStep(
                3,
                "htf_alignment",
                "上位足との整合",
                "warn",
                f"上位足({_side_ja(higher_side)})がエントリー方向({_side_ja(plan.direction)})と逆行 — 一致度{agreement:.0%}",
            )
        )
    else:
        steps.append(
            CheckStep(
                3, "htf_alignment", "上位足との整合", "ok", f"時間足の向き一致度 {agreement:.0%}"
            )
        )

    # 4. ボラティリティ確認 ---------------------------------------------------
    atr_pct = None
    close = plan.close
    if plan.atr is not None and close:
        atr_pct = plan.atr / close * 100.0
    if plan.atr is None or plan.atr <= 0:
        steps.append(
            CheckStep(
                4, "volatility", "ボラティリティ確認", "block", "ATR(1h)取得失敗 — SL/TP算出不能"
            )
        )
    elif atr_pct is not None and atr_pct < ATR_PCT_MIN:
        steps.append(
            CheckStep(
                4,
                "volatility",
                "ボラティリティ確認",
                "warn",
                f"ボラ過小(ATR {atr_pct:.3f}%) — 値動き乏しくコスト負けしやすい",
            )
        )
    elif atr_pct is not None and atr_pct > ATR_PCT_MAX:
        steps.append(
            CheckStep(
                4,
                "volatility",
                "ボラティリティ確認",
                "warn",
                f"ボラ過大(ATR {atr_pct:.2f}%) — ストップ幅が広くサイズを絞る必要",
            )
        )
    else:
        label = f"ATR {atr_pct:.3f}%" if atr_pct is not None else f"ATR {plan.atr:.5f}"
        steps.append(CheckStep(4, "volatility", "ボラティリティ確認", "ok", f"ボラ正常 ({label})"))

    # 5. 流動性・スプレッド確認 -----------------------------------------------
    spread = _spread_at(tech, "1h")
    stop_distance = None
    if plan.stop is not None and close is not None:
        stop_distance = abs(close - plan.stop)
    if spread is None:
        steps.append(
            CheckStep(
                5, "spread", "流動性・スプレッド確認", "warn", "スプレッド不明(bid/ask未取得)"
            )
        )
    elif stop_distance is None or stop_distance <= 0:
        steps.append(
            CheckStep(
                5,
                "spread",
                "流動性・スプレッド確認",
                "skip",
                "SL距離未確定のためスプレッド比を評価せず",
            )
        )
    else:
        frac = spread / stop_distance
        if frac >= SPREAD_BLOCK_FRACTION:
            steps.append(
                CheckStep(
                    5,
                    "spread",
                    "流動性・スプレッド確認",
                    "block",
                    f"スプレッドがSL距離の{frac:.0%} — コストがエッジを食い潰す",
                )
            )
        elif frac >= SPREAD_WARN_FRACTION:
            steps.append(
                CheckStep(
                    5,
                    "spread",
                    "流動性・スプレッド確認",
                    "warn",
                    f"スプレッドがSL距離の{frac:.0%} — 流動性やや薄い",
                )
            )
        else:
            steps.append(
                CheckStep(
                    5,
                    "spread",
                    "流動性・スプレッド確認",
                    "ok",
                    f"スプレッドはSL距離の{frac:.0%} — 許容内",
                )
            )

    # 6. ニュース・金利・イベント確認 -----------------------------------------
    event_note = next(
        (w for w in plan.warnings if "イベント" in w or "カレンダー" in w),
        "",
    )
    if not operational_data_ok:
        steps.append(
            CheckStep(
                6,
                "event",
                "ニュース・金利・イベント確認",
                "block",
                "運用データ鮮度ゲート: "
                + (operational_data_reason or "正常性を証明できず新規リスク停止"),
            )
        )
    elif not calendar_ok:
        steps.append(
            CheckStep(
                6,
                "event",
                "ニュース・金利・イベント確認",
                "block",
                event_note or "経済カレンダーの完全性・鮮度を証明できず新規リスク停止",
            )
        )
    elif plan.direction == "standby":
        steps.append(
            CheckStep(
                6,
                "event",
                "ニュース・金利・イベント確認",
                "block",
                event_note or "高影響イベント窓のため新規は様子見",
            )
        )
    elif event_note:
        steps.append(CheckStep(6, "event", "ニュース・金利・イベント確認", "warn", event_note))
    else:
        steps.append(
            CheckStep(
                6,
                "event",
                "ニュース・金利・イベント確認",
                "ok",
                "警戒イベント窓なし・カレンダー取得済み",
            )
        )

    # 7. 期待値計算 -----------------------------------------------------------
    target1_r = _target1_r(plan)
    bound_cost_r = execution_cost_in_r(spread, stop_distance, slippage_spreads)
    invalid_realized_expectancy = False
    if _finite_real(realized_expectancy_r):
        expected_r = round(realized_expectancy_r, 3)
        exp_src = "未検証の実測期待値(参考値)"
        expectancy_valid = False
    elif realized_expectancy_r is not None:
        expected_r = None
        exp_src = "実測期待値証拠が非有限または型不正"
        expectancy_valid = False
        invalid_realized_expectancy = True
    elif horizon_mismatch or calibration_horizon_mismatch:
        expected_r = None
        evidence_horizon = (
            calibrated_probability.horizon
            if calibrated_probability is not None
            else resolved_horizon
        )
        exp_src = f"判断ホライズン不整合(plan={plan_horizon}, calibration={evidence_horizon})"
        expectancy_valid = False
    elif (
        calibrated_probability is not None
        and directional
        and _finite_real(close)
        and _finite_real(plan.stop)
        and _finite_real(plan.target1)
        and _finite_real(bound_cost_r)
        and calibrated_probability.valid_at(
            now,
            plan.symbol,
            resolved_horizon,
            direction=plan.direction,
            entry_price=close,
            stop_price=plan.stop,
            target_price=plan.target1,
            cost_r=bound_cost_r,
        )
    ):
        probability = calibrated_probability.probability
        expected_r = round(
            probability * target1_r - (1.0 - probability),
            3,
        )
        exp_src = (
            f"分離期間で較正済み確率({calibrated_probability.calibrator_method}, "
            f"model={calibrated_probability.model_version})"
        )
        checklist.probability_calibrated = True
        if calibrated_probability.decision_authorized:
            expectancy_valid = True
        else:
            expectancy_valid = False
            exp_src += "・外部承認seal未実装のresearch-only証拠"
    elif directional:
        expected_r = estimate_expected_r(plan.direction, plan.conviction, target1_r)
        exp_src = "未較正の確信度ヒューリスティック(参考値)"
        expectancy_valid = False
    else:
        expected_r = None
        exp_src = ""
        expectancy_valid = False
    checklist.expected_r = expected_r
    checklist.expectancy_source = exp_src
    if not directional:
        steps.append(
            CheckStep(
                7, "expectancy", "期待値計算", "skip", "方向判断が無いため期待値評価をスキップ"
            )
        )
    elif expected_r is None:
        note = (
            exp_src
            if invalid_realized_expectancy or horizon_mismatch or calibration_horizon_mismatch
            else "期待値を算出できず"
        )
        steps.append(CheckStep(7, "expectancy", "期待値計算", "block", note))
    elif not expectancy_valid:
        steps.append(
            CheckStep(
                7,
                "expectancy",
                "期待値計算",
                "block",
                f"期待{expected_r:+.2f}R({exp_src}) — 未較正値を発注ゲートへ使用不可",
            )
        )
    elif expected_r <= 0:
        steps.append(
            CheckStep(
                7,
                "expectancy",
                "期待値計算",
                "block",
                f"期待{expected_r:+.2f}R({exp_src}) — 期待値が非正",
            )
        )
    else:
        steps.append(
            CheckStep(7, "expectancy", "期待値計算", "ok", f"期待{expected_r:+.2f}R({exp_src})")
        )

    # 8. 執行コスト控除 -------------------------------------------------------
    cost_r = bound_cost_r
    checklist.execution_cost_r = cost_r
    if not directional or expected_r is None:
        steps.append(
            CheckStep(
                8, "execution_cost", "執行コスト控除", "skip", "期待値が無いため控除評価をスキップ"
            )
        )
    elif cost_r is None:
        checklist.net_expected_r = None
        steps.append(
            CheckStep(
                8,
                "execution_cost",
                "執行コスト控除",
                "block",
                "スプレッド/SL距離不明でコストを控除できないため発注不可",
            )
        )
    else:
        net = round(expected_r - cost_r, 3)
        checklist.net_expected_r = net
        if net <= 0:
            steps.append(
                CheckStep(
                    8,
                    "execution_cost",
                    "執行コスト控除",
                    "block",
                    f"執行コスト {cost_r:.2f}R控除後の期待{net:+.2f}R — コスト負け",
                )
            )
        else:
            steps.append(
                CheckStep(
                    8,
                    "execution_cost",
                    "執行コスト控除",
                    "ok",
                    f"コスト {cost_r:.2f}R控除後の純期待{net:+.2f}R",
                )
            )

    # 9. ポジションサイズ決定 -------------------------------------------------
    if not directional or checklist.blocked:
        checklist.position_units = None
        steps.append(
            CheckStep(
                9,
                "position_size",
                "ポジションサイズ決定",
                "skip",
                "エントリー見送り(前段でブロック/方向無し)のためサイズ算出せず",
            )
        )
    else:
        extra_risk_price = (
            spread * (1.0 + slippage_spreads)
            if _finite_real(spread)
            and spread > 0
            and _finite_real(slippage_spreads)
            and slippage_spreads >= 0
            else 0.0
        )
        units = position_units(
            account_balance,
            plan.risk_pct,
            stop_distance,
            symbol=plan.symbol,
            entry_price=close,
            extra_risk_price=extra_risk_price,
        )
        checklist.position_units = units
        if units is None:
            if account_balance is None:
                steps.append(
                    CheckStep(
                        9,
                        "position_size",
                        "ポジションサイズ決定",
                        "block",
                        "口座残高が無く、損失上限に結合したサイズを証明できない",
                    )
                )
            else:
                steps.append(
                    CheckStep(
                        9,
                        "position_size",
                        "ポジションサイズ決定",
                        "block",
                        "SL距離・価格・通貨換算を証明できずサイズ算出不可",
                    )
                )
        else:
            steps.append(
                CheckStep(
                    9,
                    "position_size",
                    "ポジションサイズ決定",
                    "ok",
                    f"{units:,.0f}通貨単位(残高の{plan.risk_pct:.1f}%リスク / SL距離基準)",
                )
            )

    return checklist


def run_pipeline(
    symbol: str,
    tech: PairTechnicals,
    currency_scores: Mapping,
    windows: Sequence,
    news_items: Sequence,
    *,
    now: datetime | None = None,
    account_balance: float | None = None,
    slippage_spreads: float = DEFAULT_SLIPPAGE_SPREADS,
    realized_expectancy_r: float | None = None,
    calibrated_probability: CalibratedProbabilityEvidence | None = None,
    horizon: str = "24h",
    atr_multiple: float = DEFAULT_ATR_MULTIPLE,
    risk_pct: float = DEFAULT_RISK_PCT,
    calendar_ok: bool = True,
    operational_data_ok: bool = True,
    operational_data_reason: str = "",
    extra_components: Sequence[ScoreComponent] = (),
    expectancy_adjuster: Callable[[str, str, int], tuple[float, str, bool]] | None = None,
    target_r_adjuster: TargetRAdjuster | None = None,
    **plan_kwargs,
) -> tuple[TradePlan, DecisionChecklist]:
    """build_trade_plan を走らせ、その結果をチェックリストに写像して両方返す。

    既存の build_trade_plan の全機能(学習調整・委員会・期待値ガード・TP/SL
    承認)をそのまま通したうえで、順序付きチェックリストを付ける薄いラッパー。
    """
    horizon_hours = _horizon_hours_from_label(horizon)
    if "horizon_hours" in plan_kwargs:
        raise TypeError("pass horizon instead of horizon_hours to run_pipeline")
    plan = build_trade_plan(
        symbol,
        tech,
        currency_scores,
        windows,
        news_items,
        now=now,
        horizon_hours=horizon_hours,
        atr_multiple=atr_multiple,
        risk_pct=risk_pct,
        calendar_ok=calendar_ok,
        operational_data_ok=operational_data_ok,
        operational_data_reason=operational_data_reason,
        extra_components=extra_components,
        expectancy_adjuster=expectancy_adjuster,
        target_r_adjuster=target_r_adjuster,
        **plan_kwargs,
    )
    checklist = build_checklist(
        plan,
        tech,
        now=now,
        account_balance=account_balance,
        slippage_spreads=slippage_spreads,
        realized_expectancy_r=realized_expectancy_r,
        calibrated_probability=calibrated_probability,
        horizon=horizon,
        calendar_ok=calendar_ok,
        operational_data_ok=operational_data_ok,
        operational_data_reason=operational_data_reason,
    )
    plan.action = checklist.final_action
    plan.checklist = checklist.to_dict()
    return plan, checklist


def _side_ja(side: str) -> str:
    return {"long": "ロング", "short": "ショート"}.get(side, side)


def _target1_r(plan: TradePlan) -> float:
    policy = plan.target_policy or {}
    value = policy.get("target1_r")
    if _finite_real(value) and value > 0:
        return float(value)
    return DEFAULT_TARGET1_R
