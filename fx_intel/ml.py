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
    brier_score,
    log_loss,
    platt_calibrate,
)
from .learning import EvaluatedCall
from .learning import thin_calls as _thin_calls_impl
from .journal import FUSION_PIT_DATA_CONTRACT

SCHEMA_VERSION = 4
TRAINING_CONTRACT = FUSION_PIT_DATA_CONTRACT

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
) -> dict[str, float | None]:
    """判断1件を方向符号付きの特徴量辞書にする(学習・予測で共通)。

    「ロングで当たるか」と「ショートで当たるか」を同じモデルで扱えるよう、
    向きの意味を持つ特徴量に方向符号を掛けて「判断方向への追い風の強さ」に
    正規化する。取得できていない特徴量はNone(後段で中央値補完)。
    """
    sign = 1.0 if direction == "long" else -1.0
    rsi = chart.get("rsi_1h")
    ma_gap = chart.get("ma_gap_atr")
    macro = chart.get("macro_score")
    # 上位足レーティングは4h/1dの取得できた方の平均(欠損耐性を細かい重み差より優先)
    htf_values = [
        float(value)
        for value in (chart.get("rating_4h"), chart.get("rating_1d"))
        if isinstance(value, (int, float))
    ]
    htf = sum(htf_values) / len(htf_values) if htf_values else None
    return {
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
    }


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
            )
        )
        labels.append(1 if call.outcome == "hit" else 0)
        stamps.append(ts)
    return rows, labels, stamps


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

    def predict_hit_probability(
        self,
        direction: str,
        tech_score: float,
        news_score: float,
        chart: Mapping[str, float],
        data_quality: float | None = None,
    ) -> float | None:
        """P(hit | 特徴量, 方向)。usableでない・モデル無しはNone。"""
        if not self.usable or self.model is None or direction not in ("long", "short"):
            return None
        row = direction_features(direction, tech_score, news_score, chart, data_quality)
        margin = self.model.predict_margin(vectorize(row, self.medians))
        return round(self.calibration.apply(margin), 4)

    def direction_edge(
        self,
        tech_score: float,
        news_score: float,
        chart: Mapping[str, float],
        data_quality: float | None = None,
    ) -> tuple[float, float] | None:
        """(P(hit|long), P(hit|short))。committeeのML委員の入力。"""
        p_long = self.predict_hit_probability("long", tech_score, news_score, chart, data_quality)
        p_short = self.predict_hit_probability("short", tech_score, news_score, chart, data_quality)
        if p_long is None or p_short is None:
            return None
        return p_long, p_short

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
        return " | ".join(parts)


def train_artifact(
    calls: Sequence[EvaluatedCall],
    now: datetime | None = None,
    seed: int = 7,
    min_train_rows: int = MIN_TRAIN_ROWS,
) -> MLArtifact:
    """採点済み判断から学習・検証・較正まで行い、来歴付きで返す。

    データ不足・スキル不足でも例外にせず usable=False の理由付き
    アーティファクトを返す(呼び出し側は理由をそのまま表示できる)。
    """
    now = now or datetime.now(UTC)
    artifact = MLArtifact(trained_at=now.isoformat())

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
        )
        if model_raw is not None:
            artifact.model = GradientBoostingClassifier.from_dict(model_raw)
        elif artifact.usable:
            return MLArtifact(reasons=["usableなのにモデル本体が無い(破損)"])
        return artifact
    except (KeyError, TypeError, ValueError) as error:
        return MLArtifact(reasons=[f"モデルファイル破損: {error}"])
