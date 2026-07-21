"""判断ジャーナルからGBDT確率モデルを学習・運用するパイプライン。

学習データはjournal.pyが蓄積した過去の方向判断で、learning.evaluate_history が
「約24時間後に順行したか(hit/miss)」を採点済みのものを使う。モデルは
「いまの状態でこの方向に張ったら当たる確率 P(hit | 特徴量, 方向)」を出力し、
committee.py がロング/ショート両方向の確率差をML委員の意見として使う。

時系列データ特有の罠への対策(ここが本モジュールの本体):

1. 自己相関の間引き — ブリーフィングは5分周期で実行され、24時間ホライズンの
   評価窓は隣接エントリとほぼ全て重複する。ほぼ同じ判断を多数数えると
   実効サンプル数を過大評価するため、ペアごとに最低4時間空けて間引く
2. 5区間の時系列分割+エンバーゴ — train/tune/calibration/test/lockboxを
   時刻単位で分離する。各境界の72時間(評価ホライズン+週末)を前側から落とし、
   ラベル窓が次区間へ食い込むリーク(情報漏れ)を防ぐ。lockboxは学習時に評価しない
3. スキルゲート — testのBrier/loglossがcalibration区間で推定した切片のみの
   基準率予測を十分に改善し、かつtest AUCが0.55以上でないモデルは
   usable=Falseとして保存される。学習できた事実と使ってよい事実を分離する
4. 較正 — calibration区間だけでPlattスケーリングを当て、固定した変換を
   testへ適用する(確信の誇張とtest再利用を防ぐ)
5. 来歴 — 学習時刻・サンプル数・指標・特徴量重要度をアーティファクトに
   埋め込み、params_gate と同じ「来歴の無いモデルは信用しない」思想を貫く

このモジュールはネットワークアクセスを持たない純粋ロジックで、
テストから直接検証できる。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, UTC
from pathlib import Path
from collections.abc import Mapping, Sequence

from .gbm import (
    CalibrationResult,
    GradientBoostingClassifier,
    GradientBoostingRegressor,
    brier_score,
    log_loss,
    platt_calibrate,
    rmse,
)
from .learning import EvaluatedCall
from .learning import thin_calls as _thin_calls_impl
from .journal import FUSION_PIT_DATA_CONTRACT

SCHEMA_VERSION = 4
TRAINING_CONTRACT = FUSION_PIT_DATA_CONTRACT
VALID_FRACTION = 0.2

# 方向依存(判断方向の符号を掛ける)特徴量と方向非依存の特徴量
FEATURE_NAMES: tuple[str, ...] = (
    "dir_tech",  # tech_score × 方向符号
    "dir_news",  # news_score × 方向符号
    "dir_rsi",  # (RSI-50)/50 × 方向符号
    "dir_ma_gap",  # MA乖離(ATR換算) × 方向符号
    "dir_macro",  # マクロスコア × 方向符号(committee導入後に蓄積)
    "dir_htf",  # 上位足(4h/1d)レーティング平均 × 方向符号(順行+/逆行-)
    "adx_1h",
    "atr_pct",
    "tf_agreement",
    "news_count",
    "data_quality",
    "macro_vix_level",
    "macro_vix_change_5d_pct",
    "macro_us10y_change_5d_bp",
    "macro_curve_2s10s_bp",
    "macro_usd_index_change_5d_pct",
    "dir_cot_pair_diff",
    "liquidity_spread_pips",
    "liquidity_spread_bps",
    "liquidity_spread_percentile",
    "liquidity_quote_age_sec",
    "liquidity_spread_atr",
    "liquidity_is_rollover_window",
    "liquidity_status_normal",
    "liquidity_status_thin",
    "liquidity_status_stressed",
    "liquidity_status_unknown",
    "liquidity_status_invalid",
    "macro_vix_available",
    "macro_cot_available",
    "liquidity_spread_available",
    "liquidity_baseline_available",
    "session_tokyo",
    "session_tokyo_london_overlap",
    "session_london",
    "session_london_new_york_overlap",
    "session_new_york",
    "session_other_overlap",
    "session_unknown",
    "regime_risk_on",
    "regime_risk_off",
    "regime_unknown",
)

MIN_TRAIN_ROWS = 150  # 間引き後の採点済みサンプルがこれ未満なら学習しない
MIN_CLASS_ROWS = 30  # hit/missの各クラスの最低数
THIN_MIN_GAP_HOURS = 4.0  # 同一ペアの学習サンプル間の最低間隔
EMBARGO_HOURS = 72.0  # 学習/検証境界の除外幅(評価ホライズン24h+週末+余裕)
TRAIN_FRACTION = 0.50
TUNE_FRACTION = 0.15
CALIBRATION_FRACTION = 0.10
TEST_FRACTION = 0.15
LOCKBOX_FRACTION = 0.10
MIN_PARTITION_ROWS = 30
# スキルゲート: 検証Brierが基準率予測をこの相対割合以上改善しないと使わない。
# 数値誤差やノイズへの過学習でわずかに勝っただけのモデルを弾く安全マージン。
# ミッションクリティカル用途では「勝ったかもしれない」を「有効」と扱わない
MIN_BRIER_IMPROVEMENT = 0.02  # 2%以上の改善を要求
MIN_TEST_AUC = 0.55  # 基準率シフトだけでは得られない順位識別力を要求
MIN_RETURN_ROWS = 80
MIN_RETURN_VALID_ROWS = 20
MIN_RETURN_TSTAT = 2.0
MIN_RETURN_DSR = 0.95
RETURN_QUANTILES = (0.1, 0.5, 0.9)

DEFAULT_MODEL_PATH = "logs/ml_model.json"


def _parse_ts(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def direction_features(
    direction: str,
    tech_score: float,
    news_score: float,
    chart: Mapping[str, float],
    data_quality: float | None = None,
    dimensions: Mapping[str, object] | None = None,
) -> dict[str, float | None]:
    """判断1件を方向符号付きの特徴量辞書にする(学習・予測で共通)。

    「ロングで当たるか」と「ショートで当たるか」を同じモデルで扱えるよう、
    向きの意味を持つ特徴量に方向符号を掛けて「判断方向への追い風の強さ」に
    正規化する。取得できていない特徴量はNone(後段で中央値補完)。
    """
    sign = 1.0 if direction == "long" else -1.0
    rsi = chart.get("rsi_1h")
    ma_gap = chart.get("ma_gap_atr")
    macro = chart.get("macro_score", chart.get("macro__macro_pair_score"))
    cot_pair_diff = _maybe_float(chart.get("macro__cot_pair_diff"))
    # 上位足レーティングは4h/1dの取得できた方の平均(欠損耐性を細かい重み差より優先)
    htf_values = [
        float(value)
        for value in (chart.get("rating_4h"), chart.get("rating_1d"))
        if isinstance(value, (int, float))
    ]
    htf = sum(htf_values) / len(htf_values) if htf_values else None
    values: dict[str, float | None] = {
        "dir_tech": tech_score * sign,
        "dir_news": news_score * sign,
        "dir_rsi": ((float(rsi) - 50.0) / 50.0) * sign if isinstance(rsi, (int, float)) else None,
        "dir_ma_gap": float(ma_gap) * sign if isinstance(ma_gap, (int, float)) else None,
        "dir_macro": float(macro) * sign if isinstance(macro, (int, float)) else None,
        "dir_htf": htf * sign if htf is not None else None,
        "adx_1h": _maybe_float(chart.get("adx_1h")),
        "atr_pct": _maybe_float(chart.get("atr_pct")),
        "tf_agreement": _maybe_float(chart.get("tf_agreement")),
        "news_count": _maybe_float(chart.get("news_count")),
        "data_quality": float(data_quality) if isinstance(data_quality, (int, float)) else None,
        "macro_vix_level": _maybe_float(chart.get("macro__vix_level")),
        "macro_vix_change_5d_pct": _maybe_float(chart.get("macro__vix_change_5d_pct")),
        "macro_us10y_change_5d_bp": _maybe_float(chart.get("macro__us10y_change_5d_bp")),
        "macro_curve_2s10s_bp": _maybe_float(chart.get("macro__curve_2s10s_bp")),
        "macro_usd_index_change_5d_pct": _maybe_float(chart.get("macro__usd_index_change_5d_pct")),
        "dir_cot_pair_diff": cot_pair_diff * sign if cot_pair_diff is not None else None,
        "liquidity_spread_pips": _maybe_float(chart.get("liquidity__spread_pips")),
        "liquidity_spread_bps": _maybe_float(chart.get("liquidity__spread_bps")),
        "liquidity_spread_percentile": _maybe_float(chart.get("liquidity__spread_percentile")),
        "liquidity_quote_age_sec": _maybe_float(chart.get("liquidity__quote_age_sec")),
        "liquidity_spread_atr": _maybe_float(chart.get("liquidity__spread_atr")),
        "liquidity_is_rollover_window": _maybe_float(chart.get("liquidity__is_rollover_window")),
        "liquidity_status_normal": _maybe_float(chart.get("liquidity__status_normal")),
        "liquidity_status_thin": _maybe_float(chart.get("liquidity__status_thin")),
        "liquidity_status_stressed": _maybe_float(chart.get("liquidity__status_stressed")),
        "liquidity_status_unknown": _maybe_float(chart.get("liquidity__status_unknown")),
        "liquidity_status_invalid": _maybe_float(chart.get("liquidity__status_invalid")),
        "macro_vix_available": _maybe_float(chart.get("macro__vix_level__available")),
        "macro_cot_available": _maybe_float(chart.get("macro__cot_pair_diff__available")),
        "liquidity_spread_available": _maybe_float(chart.get("liquidity__spread_pips__available")),
        "liquidity_baseline_available": _maybe_float(
            chart.get("liquidity__spread_percentile__available")
        ),
    }
    dimensions = dimensions or {}
    session = str(dimensions.get("session_bucket", "unknown") or "unknown")
    regime = str(dimensions.get("regime", "unknown") or "unknown")
    for bucket in (
        "tokyo",
        "tokyo_london_overlap",
        "london",
        "london_new_york_overlap",
        "new_york",
        "other_overlap",
        "unknown",
    ):
        values[f"session_{bucket}"] = 1.0 if session == bucket else 0.0
    for bucket in ("risk_on", "risk_off", "unknown"):
        values[f"regime_{bucket}"] = 1.0 if regime == bucket else 0.0
    return values


def _maybe_float(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def thin_calls(
    calls: Sequence[EvaluatedCall], min_gap_hours: float = THIN_MIN_GAP_HOURS
) -> list[EvaluatedCall]:
    """自己相関対策の間引き(実装はlearning.thin_callsを共用)。

    5分周期×24時間ホライズンでは隣接判断の評価窓がほぼ重複しており、
    全部使うと「n件の独立サンプル」を装った1件分の情報になるため。
    ML学習は学習プロファイル(1時間)より厳しい4時間間隔を既定にする。
    """
    return _thin_calls_impl(calls, min_gap_hours)


def build_dataset(
    calls: Sequence[EvaluatedCall],
) -> tuple[list[dict[str, float | None]], list[int], list[datetime]]:
    """採点済み判断(hit/miss)を特徴量辞書・ラベル・時刻の3列に変換する。"""
    rows: list[dict[str, float | None]] = []
    labels: list[int] = []
    stamps: list[datetime] = []
    for call in calls:
        if call.outcome not in ("hit", "miss") or call.direction not in ("long", "short"):
            continue
        ts = _parse_ts(call.ts)
        if ts is None:
            continue
        rows.append(
            direction_features(
                call.direction,
                call.tech_score,
                call.news_score,
                call.features,
                data_quality=call.data_quality,
                dimensions=call.dimensions,
            )
        )
        labels.append(1 if call.outcome == "hit" else 0)
        stamps.append(ts)
    return rows, labels, stamps


def build_return_dataset(
    outcomes: Sequence[Mapping[str, object]],
) -> tuple[
    list[dict[str, float | None]],
    list[float],
    list[datetime],
    str,
    str,
]:
    """Build a return dataset only from canonical, eligible net-R labels."""

    materialized: list[tuple[datetime, Mapping[str, object]]] = []
    versions: set[str] = set()
    cost_models: set[str] = set()
    for outcome in outcomes:
        net_r = _maybe_float(outcome.get("realized_net_r"))
        ts = _parse_ts(str(outcome.get("ts", "")))
        direction = str(outcome.get("direction", ""))
        eligible = bool(outcome.get("net_label_eligible", outcome.get("tradable", False)))
        if net_r is None or ts is None or direction not in ("long", "short") or not eligible:
            continue
        version = str(outcome.get("label_version", "")).strip()
        cost_model = str(outcome.get("cost_model_id", "")).strip()
        if not version or not cost_model:
            continue
        versions.add(version)
        cost_models.add(cost_model)
        materialized.append((ts, outcome))
    if len(versions) > 1 or len(cost_models) > 1:
        raise ValueError("label_version または cost_model_id が混在")

    rows: list[dict[str, float | None]] = []
    labels: list[float] = []
    stamps: list[datetime] = []
    last_kept: dict[str, datetime] = {}
    for ts, outcome in sorted(materialized, key=lambda item: item[0]):
        symbol = str(outcome.get("symbol", ""))
        previous = last_kept.get(symbol)
        if previous is not None and (ts - previous) < timedelta(hours=THIN_MIN_GAP_HOURS):
            continue
        last_kept[symbol] = ts
        raw_features = outcome.get("features")
        chart = (
            {
                str(key): float(value)
                for key, value in raw_features.items()
                if isinstance(value, (int, float))
            }
            if isinstance(raw_features, Mapping)
            else {}
        )
        raw_dimensions = outcome.get("learning_dimensions")
        dimensions = raw_dimensions if isinstance(raw_dimensions, Mapping) else None
        rows.append(
            direction_features(
                str(outcome.get("direction")),
                _maybe_float(outcome.get("tech_score")) or 0.0,
                _maybe_float(outcome.get("news_score")) or 0.0,
                chart,
                _maybe_float(outcome.get("data_quality")),
                dimensions=dimensions,
            )
        )
        label = _maybe_float(outcome.get("realized_net_r"))
        assert label is not None  # materialized rows were filtered above
        labels.append(label)
        stamps.append(ts)
    return (
        rows,
        labels,
        stamps,
        next(iter(versions), ""),
        next(iter(cost_models), ""),
    )


def _median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def compute_medians(rows: Sequence[Mapping[str, float | None]]) -> dict[str, float]:
    """特徴量ごとの中央値(欠損補完用)。全欠損の特徴量は0.0。"""
    medians: dict[str, float] = {}
    for name in FEATURE_NAMES:
        present = [float(value) for row in rows if (value := row.get(name)) is not None]
        medians[name] = round(_median(present), 6) if present else 0.0
    return medians


def vectorize(row: Mapping[str, float | None], medians: Mapping[str, float]) -> list[float]:
    """特徴量辞書を学習時と同じ列順のベクトルにする(欠損は中央値補完)。"""
    vector: list[float] = []
    for name in FEATURE_NAMES:
        value = row.get(name)
        if value is None or not math.isfinite(float(value)):
            value = medians.get(name, 0.0)
        vector.append(float(value))
    return vector


@dataclass
class MLArtifact:
    """学習済みモデル+来歴+較正のセット。usable=Falseは判断に使わない。"""

    trained_at: str = ""
    n_train: int = 0
    n_valid: int = 0
    n_tune: int = 0
    n_calibration: int = 0
    n_test: int = 0
    n_lockbox: int = 0
    lockbox_evaluated: bool = False
    partition_windows: dict[str, dict[str, object]] = field(default_factory=dict)
    base_rate: float = 0.5
    calibration_base_rate: float = 0.5
    medians: dict[str, float] = field(default_factory=dict)
    val_logloss: float | None = None
    baseline_logloss: float | None = None
    val_brier: float | None = None
    baseline_brier: float | None = None
    test_auc: float | None = None
    usable: bool = False
    reasons: list[str] = field(default_factory=list)
    model: GradientBoostingClassifier | None = None
    calibration: CalibrationResult = field(default_factory=CalibrationResult)
    importance_by_name: dict[str, float] = field(default_factory=dict)
    return_model: GradientBoostingRegressor | None = None
    return_medians: dict[str, float] = field(default_factory=dict)
    quantile_models: dict[str, GradientBoostingRegressor] = field(default_factory=dict)
    return_n_train: int = 0
    return_n_valid: int = 0
    return_val_rmse: float | None = None
    return_baseline_rmse: float | None = None
    return_oos_mean_net_r: float | None = None
    return_oos_r_tstat: float | None = None
    return_dsr: float | None = None
    return_usable: bool = False
    return_label_version: str = ""
    return_cost_model_id: str = ""
    return_reasons: list[str] = field(default_factory=list)

    def predict_hit_probability(
        self,
        direction: str,
        tech_score: float,
        news_score: float,
        chart: Mapping[str, float],
        data_quality: float | None = None,
        dimensions: Mapping[str, object] | None = None,
    ) -> float | None:
        """P(hit | 特徴量, 方向)。usableでない・モデル無しはNone。"""
        if not self.usable or self.model is None or direction not in ("long", "short"):
            return None
        row = direction_features(direction, tech_score, news_score, chart, data_quality, dimensions)
        margin = self.model.predict_margin(vectorize(row, self.medians))
        return round(self.calibration.apply(margin), 4)

    def direction_edge(
        self,
        tech_score: float,
        news_score: float,
        chart: Mapping[str, float],
        data_quality: float | None = None,
        dimensions: Mapping[str, object] | None = None,
    ) -> tuple[float, float] | None:
        """(P(hit|long), P(hit|short))。committeeのML委員の入力。"""
        p_long = self.predict_hit_probability(
            "long", tech_score, news_score, chart, data_quality, dimensions
        )
        p_short = self.predict_hit_probability(
            "short", tech_score, news_score, chart, data_quality, dimensions
        )
        if p_long is None or p_short is None:
            return None
        return p_long, p_short

    def expected_net_r(
        self,
        direction: str,
        tech_score: float,
        news_score: float,
        chart: Mapping[str, float],
        data_quality: float | None = None,
        dimensions: Mapping[str, object] | None = None,
    ) -> float | None:
        if not self.return_usable or self.return_model is None:
            return None
        row = direction_features(direction, tech_score, news_score, chart, data_quality, dimensions)
        return round(self.return_model.predict(vectorize(row, self.return_medians)), 4)

    def net_r_interval(
        self,
        direction: str,
        tech_score: float,
        news_score: float,
        chart: Mapping[str, float],
        data_quality: float | None = None,
        dimensions: Mapping[str, object] | None = None,
    ) -> tuple[float, float, float] | None:
        if not self.return_usable or not self.quantile_models:
            return None
        row = vectorize(
            direction_features(direction, tech_score, news_score, chart, data_quality, dimensions),
            self.return_medians,
        )
        predictions = sorted(model.predict(row) for model in self.quantile_models.values())
        if len(predictions) != 3:
            return None
        return (
            round(predictions[0], 4),
            round(predictions[1], 4),
            round(predictions[2], 4),
        )

    def summary_ja(self) -> str:
        if self.model is None:
            return "MLモデル未学習"
        status = "有効" if self.usable else "無効(スキルゲート不合格)"
        parts = [
            f"MLモデル[{status}] 学習{self.n_train}件/調整{self.n_tune}件/"
            f"較正{self.n_calibration}件/test{self.n_test}件/lockbox保留{self.n_lockbox}件",
        ]
        if self.val_brier is not None and self.baseline_brier is not None:
            parts.append(f"Brier {self.val_brier:.3f}(基準率予測 {self.baseline_brier:.3f})")
        if self.reasons:
            parts.append("; ".join(self.reasons))
        if self.return_model is not None:
            return_status = "shadow有効" if self.return_usable else "shadow無効"
            parts.append(
                f"純R[{return_status}] train {self.return_n_train}/valid {self.return_n_valid}"
            )
        return " | ".join(parts)


def train_artifact(
    calls: Sequence[EvaluatedCall],
    return_outcomes: Sequence[Mapping[str, object]] = (),
    now: datetime | None = None,
    seed: int = 7,
    min_train_rows: int = MIN_TRAIN_ROWS,
    min_return_rows: int = MIN_RETURN_ROWS,
) -> MLArtifact:
    """採点済み判断から学習・検証・較正まで行い、来歴付きで返す。

    データ不足・スキル不足でも例外にせず usable=False の理由付き
    アーティファクトを返す(呼び出し側は理由をそのまま表示できる)。
    """
    now = now or datetime.now(UTC)
    artifact = MLArtifact(trained_at=now.isoformat())
    _train_return_head(
        artifact,
        return_outcomes,
        seed=seed,
        min_return_rows=min_return_rows,
    )

    thinned = thin_calls(calls)
    rows, labels, stamps = build_dataset(thinned)
    if len(rows) < min_train_rows:
        artifact.reasons.append(f"学習サンプル不足(間引き後{len(rows)}件 < {min_train_rows}件)")
        return artifact
    hit_count = sum(labels)
    if hit_count < MIN_CLASS_ROWS or len(labels) - hit_count < MIN_CLASS_ROWS:
        artifact.reasons.append(
            f"クラス偏り(hit {hit_count} / miss {len(labels) - hit_count}、"
            f"各{MIN_CLASS_ROWS}件必要)"
        )
        return artifact

    # train/tune/calibration/test/lockbox を完全分離する。同時刻の複数ペアは
    # 同じpartitionへ置き、各境界の学習側末尾を72時間embargoする。
    order = sorted(range(len(rows)), key=lambda i: stamps[i])
    rows = [rows[i] for i in order]
    labels = [labels[i] for i in order]
    stamps = [stamps[i] for i in order]
    partitions = _temporal_partitions(stamps)
    for name in ("train", "tune", "calibration", "test", "lockbox"):
        if len(partitions[name]) < MIN_PARTITION_ROWS:
            artifact.reasons.append(
                f"{name}サンプル不足({len(partitions[name])}件 < {MIN_PARTITION_ROWS}件)"
            )
            return artifact
        if len({labels[index] for index in partitions[name]}) < 2:
            artifact.reasons.append(f"{name}セットが単一クラス")
            return artifact

    train_idx = partitions["train"]
    tune_idx = partitions["tune"]
    calibration_idx = partitions["calibration"]
    test_idx = partitions["test"]
    lockbox_idx = partitions["lockbox"]

    medians = compute_medians([rows[i] for i in train_idx])
    x_train = [vectorize(rows[i], medians) for i in train_idx]
    y_train = [labels[i] for i in train_idx]
    x_tune = [vectorize(rows[i], medians) for i in tune_idx]
    y_tune = [labels[i] for i in tune_idx]
    x_calibration = [vectorize(rows[i], medians) for i in calibration_idx]
    y_calibration = [labels[i] for i in calibration_idx]
    x_test = [vectorize(rows[i], medians) for i in test_idx]
    y_test = [labels[i] for i in test_idx]

    model = GradientBoostingClassifier(seed=seed)
    model.fit(x_train, y_train, x_tune, y_tune)

    calibration_margins = [model.predict_margin(row) for row in x_calibration]
    calibration = platt_calibrate(calibration_margins, y_calibration)
    test_margins = [model.predict_margin(row) for row in x_test]
    probs = [calibration.apply(margin) for margin in test_margins]
    base_rate = sum(y_train) / len(y_train)
    calibration_base_rate = sum(y_calibration) / len(y_calibration)
    # The correct null is an intercept fitted on the same calibration window.
    # A train-prevalence baseline lets calibration-period regime shift masquerade
    # as feature skill even when every predictor is constant.
    baseline = [calibration_base_rate] * len(y_test)

    artifact.n_train = len(x_train)
    artifact.n_valid = len(x_test)  # schema-1 reader/display compatibility
    artifact.n_tune = len(x_tune)
    artifact.n_calibration = len(x_calibration)
    artifact.n_test = len(x_test)
    artifact.n_lockbox = len(lockbox_idx)
    artifact.lockbox_evaluated = False
    artifact.partition_windows = {
        name: _partition_window(stamps, indices) for name, indices in partitions.items()
    }
    artifact.base_rate = round(base_rate, 4)
    artifact.calibration_base_rate = round(calibration_base_rate, 4)
    artifact.medians = medians
    artifact.model = model
    artifact.calibration = calibration
    artifact.val_logloss = round(log_loss(y_test, probs), 6)
    artifact.baseline_logloss = round(log_loss(y_test, baseline), 6)
    artifact.val_brier = round(brier_score(y_test, probs), 6)
    artifact.baseline_brier = round(brier_score(y_test, baseline), 6)
    artifact.test_auc = round(_binary_auc(y_test, probs), 6)
    artifact.importance_by_name = {
        FEATURE_NAMES[index]: round(gain, 4)
        for index, gain in sorted(model.feature_importance_.items(), key=lambda kv: -kv[1])
    }

    # testスキルゲート: 基準率予測をBrier/loglossの両方で、かつ有意なマージンで
    # 上回るモデルだけを usable にする。誤差レベルの改善は「勝った」扱いしない
    brier_gain = (artifact.baseline_brier - artifact.val_brier) / artifact.baseline_brier
    logloss_better = artifact.val_logloss < artifact.baseline_logloss
    discrimination_ok = artifact.test_auc >= MIN_TEST_AUC
    has_feature_signal = bool(artifact.importance_by_name)
    if (
        brier_gain >= MIN_BRIER_IMPROVEMENT
        and logloss_better
        and discrimination_ok
        and has_feature_signal
    ):
        artifact.usable = True
    else:
        artifact.reasons.append(
            "スキルゲート不合格: 検証Brier改善が基準率予測比"
            f"{brier_gain:+.1%}(要{MIN_BRIER_IMPROVEMENT:.0%})"
            f" / logloss {'改善' if logloss_better else '未改善'}"
            f" / AUC {artifact.test_auc:.3f}(要{MIN_TEST_AUC:.2f})"
            f" / 特徴量識別 {'あり' if has_feature_signal else 'なし'}"
            f"(Brier {artifact.val_brier:.3f} vs {artifact.baseline_brier:.3f})"
        )
    return artifact


def _temporal_partitions(stamps: Sequence[datetime]) -> dict[str, list[int]]:
    fractions = (
        TRAIN_FRACTION,
        TRAIN_FRACTION + TUNE_FRACTION,
        TRAIN_FRACTION + TUNE_FRACTION + CALIBRATION_FRACTION,
        TRAIN_FRACTION + TUNE_FRACTION + CALIBRATION_FRACTION + TEST_FRACTION,
    )
    boundaries = [0]
    for fraction in fractions:
        boundary = min(len(stamps) - 1, max(1, int(len(stamps) * fraction)))
        while boundary < len(stamps) and stamps[boundary] == stamps[boundary - 1]:
            boundary += 1
        boundaries.append(boundary)
    boundaries.append(len(stamps))
    names = ("train", "tune", "calibration", "test", "lockbox")
    partitions: dict[str, list[int]] = {}
    embargo = timedelta(hours=EMBARGO_HOURS)
    for offset, name in enumerate(names):
        start, stop = boundaries[offset], boundaries[offset + 1]
        indices = list(range(start, stop))
        if name != "lockbox":
            next_start = stamps[stop]
            indices = [index for index in indices if stamps[index] < next_start - embargo]
        partitions[name] = indices
    return partitions


def _partition_window(stamps: Sequence[datetime], indices: Sequence[int]) -> dict[str, object]:
    return {
        "rows": len(indices),
        "start": stamps[indices[0]].isoformat(),
        "end": stamps[indices[-1]].isoformat(),
    }


def _binary_auc(labels: Sequence[int], probabilities: Sequence[float]) -> float:
    """Tie-aware ROC AUC via pairwise concordance."""

    positives = [score for label, score in zip(labels, probabilities, strict=True) if label == 1]
    negatives = [score for label, score in zip(labels, probabilities, strict=True) if label == 0]
    if not positives or not negatives:
        raise ValueError("AUC requires both classes")
    concordance = 0.0
    for positive in positives:
        for negative in negatives:
            if positive > negative:
                concordance += 1.0
            elif positive == negative:
                concordance += 0.5
    return concordance / (len(positives) * len(negatives))
def _train_return_head(
    artifact: MLArtifact,
    outcomes: Sequence[Mapping[str, object]],
    *,
    seed: int,
    min_return_rows: int,
) -> None:
    if not outcomes:
        artifact.return_reasons.append("canonical純Rラベルなし")
        return
    try:
        rows, labels, stamps, label_version, cost_model_id = build_return_dataset(outcomes)
    except ValueError as error:
        artifact.return_reasons.append(str(error))
        return
    artifact.return_label_version = label_version
    artifact.return_cost_model_id = cost_model_id
    if len(rows) < min_return_rows:
        artifact.return_reasons.append(
            f"純Rサンプル不足(間引き後{len(rows)}件 < {min_return_rows}件)"
        )
        return
    order = sorted(range(len(rows)), key=lambda index: stamps[index])
    rows = [rows[index] for index in order]
    labels = [labels[index] for index in order]
    stamps = [stamps[index] for index in order]
    split = max(1, int(len(rows) * (1.0 - VALID_FRACTION)))
    if split >= len(rows):
        artifact.return_reasons.append("純R検証区間なし")
        return
    valid_start = stamps[split]
    embargo_cut = valid_start - timedelta(hours=EMBARGO_HOURS)
    train_idx = [index for index in range(split) if stamps[index] < embargo_cut]
    valid_idx = list(range(split, len(rows)))
    min_valid = min(MIN_RETURN_VALID_ROWS, max(5, min_return_rows // 4))
    if len(train_idx) < max(20, min_return_rows // 2) or len(valid_idx) < min_valid:
        artifact.return_reasons.append(
            f"純R時系列分割後の件数不足(train {len(train_idx)}/valid {len(valid_idx)})"
        )
        return

    medians = compute_medians([rows[index] for index in train_idx])
    x_train = [vectorize(rows[index], medians) for index in train_idx]
    y_train = [labels[index] for index in train_idx]
    x_valid = [vectorize(rows[index], medians) for index in valid_idx]
    y_valid = [labels[index] for index in valid_idx]
    min_leaf = max(5, min(20, len(x_train) // 10))
    model = GradientBoostingRegressor(seed=seed, min_samples_leaf=min_leaf)
    model.fit(x_train, y_train, x_valid, y_valid)
    predictions = model.predict_many(x_valid)
    baseline_value = sum(y_train) / len(y_train)
    baseline_predictions = [baseline_value] * len(y_valid)
    artifact.return_model = model
    artifact.return_medians = medians
    artifact.return_n_train = len(x_train)
    artifact.return_n_valid = len(x_valid)
    artifact.return_val_rmse = round(rmse(y_valid, predictions), 6)
    artifact.return_baseline_rmse = round(rmse(y_valid, baseline_predictions), 6)

    for quantile in RETURN_QUANTILES:
        quantile_model = GradientBoostingRegressor(
            objective="quantile",
            quantile=quantile,
            seed=seed + int(quantile * 100),
            min_samples_leaf=min_leaf,
        )
        quantile_model.fit(x_train, y_train, x_valid, y_valid)
        artifact.quantile_models[f"p{int(quantile * 100):02d}"] = quantile_model

    selected_returns = [label for label, prediction in zip(y_valid, predictions) if prediction > 0]
    if len(selected_returns) < min_valid:
        artifact.return_reasons.append(
            f"正の期待純Rを予測したOOSサンプル不足({len(selected_returns)}件)"
        )
        return
    mean_return = sum(selected_returns) / len(selected_returns)
    variance = sum((value - mean_return) ** 2 for value in selected_returns) / max(
        1, len(selected_returns) - 1
    )
    std = math.sqrt(variance)
    tstat = mean_return / (std / math.sqrt(len(selected_returns))) if std > 0 else 0.0
    artifact.return_oos_mean_net_r = round(mean_return, 6)
    artifact.return_oos_r_tstat = round(tstat, 4)
    try:
        from fx_backtester.overfitting import deflated_sharpe_ratio

        sharpe = mean_return / std if std > 0 else 0.0
        artifact.return_dsr = round(
            float(deflated_sharpe_ratio(selected_returns, [sharpe])["dsr"]), 6
        )
    except (ImportError, TypeError, ValueError, KeyError):
        artifact.return_dsr = None
        artifact.return_reasons.append("純R DSRを計算できません")

    rmse_better = (
        artifact.return_val_rmse is not None
        and artifact.return_baseline_rmse is not None
        and artifact.return_val_rmse < artifact.return_baseline_rmse
    )
    if (
        rmse_better
        and tstat >= MIN_RETURN_TSTAT
        and artifact.return_dsr is not None
        and artifact.return_dsr >= MIN_RETURN_DSR
    ):
        artifact.return_usable = True
    else:
        artifact.return_reasons.append(
            "純R shadowゲート不合格: "
            f"RMSE {'改善' if rmse_better else '未改善'} / "
            f"t={tstat:.2f}(要{MIN_RETURN_TSTAT:.2f}) / "
            f"DSR={artifact.return_dsr if artifact.return_dsr is not None else '—'}"
        )


# ---------------------------------------------------------------- 保存/読込


def save_artifact(artifact: MLArtifact, path: str | Path) -> None:
    payload = {
        "schema": SCHEMA_VERSION,
        "training_contract": TRAINING_CONTRACT,
        "trained_at": artifact.trained_at,
        "n_train": artifact.n_train,
        "n_valid": artifact.n_valid,
        "n_tune": artifact.n_tune,
        "n_calibration": artifact.n_calibration,
        "n_test": artifact.n_test,
        "n_lockbox": artifact.n_lockbox,
        "lockbox_evaluated": artifact.lockbox_evaluated,
        "partition_windows": artifact.partition_windows,
        "base_rate": artifact.base_rate,
        "calibration_base_rate": artifact.calibration_base_rate,
        "feature_names": list(FEATURE_NAMES),
        "medians": artifact.medians,
        "metrics": {
            "val_logloss": artifact.val_logloss,
            "baseline_logloss": artifact.baseline_logloss,
            "val_brier": artifact.val_brier,
            "baseline_brier": artifact.baseline_brier,
            "test_auc": artifact.test_auc,
        },
        "usable": artifact.usable,
        "reasons": artifact.reasons,
        "calibration": {
            "scale": artifact.calibration.scale,
            "offset": artifact.calibration.offset,
        },
        "importance_by_name": artifact.importance_by_name,
        "model": artifact.model.to_dict() if artifact.model is not None else None,
        "return_head": {
            "n_train": artifact.return_n_train,
            "n_valid": artifact.return_n_valid,
            "val_rmse": artifact.return_val_rmse,
            "baseline_rmse": artifact.return_baseline_rmse,
            "oos_mean_net_r": artifact.return_oos_mean_net_r,
            "oos_r_tstat": artifact.return_oos_r_tstat,
            "dsr": artifact.return_dsr,
            "usable": artifact.return_usable,
            "label_version": artifact.return_label_version,
            "cost_model_id": artifact.return_cost_model_id,
            "reasons": artifact.return_reasons,
            "medians": artifact.return_medians,
            "model": artifact.return_model.to_dict() if artifact.return_model is not None else None,
            "quantile_models": {
                key: model.to_dict() for key, model in artifact.quantile_models.items()
            },
        },
    }
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def load_artifact(path: str | Path) -> MLArtifact:
    """保存済みモデルを読む。無い/壊れている/特徴量不一致は未学習扱い。"""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return MLArtifact()
    if not isinstance(payload, dict) or payload.get("schema") != SCHEMA_VERSION:
        return MLArtifact(reasons=["モデルファイルのスキーマ不一致"])
    if payload.get("training_contract") != TRAINING_CONTRACT:
        return MLArtifact(reasons=["モデルのPIT学習契約が現行と不一致(要再学習)"])
    if payload.get("feature_names") != list(FEATURE_NAMES):
        # 特徴量定義が変わった古いモデルを黙って使うと列がずれるため拒否
        return MLArtifact(reasons=["特徴量定義が現行と不一致(要再学習)"])
    try:
        metrics = payload.get("metrics") or {}
        calibration_raw = payload.get("calibration") or {}
        model_raw = payload.get("model")
        return_raw = payload.get("return_head") or {}
        artifact = MLArtifact(
            trained_at=str(payload.get("trained_at", "")),
            n_train=int(payload.get("n_train", 0)),
            n_valid=int(payload.get("n_valid", 0)),
            n_tune=int(payload.get("n_tune", 0)),
            n_calibration=int(payload.get("n_calibration", 0)),
            n_test=int(payload.get("n_test", 0)),
            n_lockbox=int(payload.get("n_lockbox", 0)),
            lockbox_evaluated=bool(payload.get("lockbox_evaluated", False)),
            partition_windows={
                str(name): dict(window)
                for name, window in dict(payload.get("partition_windows", {})).items()
            },
            base_rate=float(payload.get("base_rate", 0.5)),
            calibration_base_rate=float(payload.get("calibration_base_rate", 0.5)),
            medians={str(k): float(v) for k, v in dict(payload.get("medians", {})).items()},
            val_logloss=_maybe_float(metrics.get("val_logloss")),
            baseline_logloss=_maybe_float(metrics.get("baseline_logloss")),
            val_brier=_maybe_float(metrics.get("val_brier")),
            baseline_brier=_maybe_float(metrics.get("baseline_brier")),
            test_auc=_maybe_float(metrics.get("test_auc")),
            usable=bool(payload.get("usable", False)),
            reasons=[str(r) for r in payload.get("reasons", [])],
            calibration=CalibrationResult(
                scale=float(calibration_raw.get("scale", 1.0)),
                offset=float(calibration_raw.get("offset", 0.0)),
            ),
            importance_by_name={
                str(k): float(v) for k, v in dict(payload.get("importance_by_name", {})).items()
            },
            return_n_train=int(return_raw.get("n_train", 0)),
            return_n_valid=int(return_raw.get("n_valid", 0)),
            return_val_rmse=_maybe_float(return_raw.get("val_rmse")),
            return_baseline_rmse=_maybe_float(return_raw.get("baseline_rmse")),
            return_oos_mean_net_r=_maybe_float(return_raw.get("oos_mean_net_r")),
            return_oos_r_tstat=_maybe_float(return_raw.get("oos_r_tstat")),
            return_dsr=_maybe_float(return_raw.get("dsr")),
            return_usable=bool(return_raw.get("usable", False)),
            return_label_version=str(return_raw.get("label_version", "")),
            return_cost_model_id=str(return_raw.get("cost_model_id", "")),
            return_reasons=[str(reason) for reason in return_raw.get("reasons", [])],
            return_medians={
                str(key): float(value) for key, value in dict(return_raw.get("medians", {})).items()
            },
        )
        if model_raw is not None:
            artifact.model = GradientBoostingClassifier.from_dict(model_raw)
        elif artifact.usable:
            return MLArtifact(reasons=["usableなのにモデル本体が無い(破損)"])
        return_model_raw = return_raw.get("model")
        if return_model_raw is not None:
            artifact.return_model = GradientBoostingRegressor.from_dict(return_model_raw)
        elif artifact.return_usable:
            return MLArtifact(reasons=["return_usableなのに純Rモデル本体が無い(破損)"])
        raw_quantiles = return_raw.get("quantile_models", {})
        if isinstance(raw_quantiles, Mapping):
            artifact.quantile_models = {
                str(key): GradientBoostingRegressor.from_dict(value)
                for key, value in raw_quantiles.items()
                if isinstance(value, Mapping)
            }
        return artifact
    except (KeyError, TypeError, ValueError) as error:
        return MLArtifact(reasons=[f"モデルファイル破損: {error}"])
