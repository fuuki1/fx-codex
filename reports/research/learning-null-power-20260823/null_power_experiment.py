"""状態×方向セル学習の偽陽性率と検出力を、本番コードそのままで測る。

`fx_intel.learning.derive_profile` は「バケット×方向」のセルごとに
`MIN_CONDITION_SAMPLES` 件以上かつ的中率 `CONDITION_HIT_RATE_TRIGGER` 未満で
確信度の減衰係数を作る。多重比較の補正は入っていないため、真の優位が
まったく無い入力でも減衰係数が生成され得る。

この実験は2つを測る。

1. null: 的中/不的中を毎回コイン投げ(p=0.5)で決め、特徴量とは完全に独立にする。
   ここで生成された減衰セルは定義上すべて偽陽性である。
2. power: 「RSI(1h)売られすぎ圏 × ロング」だけ真に的中率が低い(p=0.40/0.45)
   分布にし、そのセルを検出できる割合と、同時に生成される偽陽性セル数を測る。

特徴量分布は運用値そのものではなく、相場でありがちな形(RSIは50中心の正規、
ATR%は対数正規など)を置いた合成である。したがって絶対値ではなく
「同じ入力規模でどれだけ偽陽性が出るか」という桁の比較に使う。

実行例:

    .venv/bin/python reports/research/learning-null-power-20260823/null_power_experiment.py
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
from collections.abc import Mapping
from datetime import datetime, timedelta, UTC

from fx_intel import learning
from fx_intel.briefing import TECH_WEIGHT

# 1時間間隔で並べると derive_profile の間引き(1時間1件)を全件が通過する。
# 実運用の融合判断(約1時間周期)・時間足別判断(間引き後1時間1件)と同じ密度。
SLICE_START = datetime(2026, 1, 5, tzinfo=UTC)
MARKET_HOURS_PER_WEEK = 120.0
TRUE_BAD_BUCKET = "売られすぎ圏(35未満)"
SAMPLE_SIZES = (200, 500, 1000, 2000, 4000)
SEEDS = 40
PROBES = 3000


def sample_features(rng: random.Random) -> dict[str, float]:
    """相場でありがちな分布から判断1件ぶんの特徴量を引く。"""
    return {
        "rsi_1h": max(1.0, min(99.0, rng.gauss(50.0, 13.0))),
        "ma_gap_atr": abs(rng.gauss(0.0, 1.1)),
        "atr_pct": max(0.01, rng.lognormvariate(-2.2, 0.55)),
        "tf_agreement": rng.choice([0.25, 0.5, 0.5, 0.75, 0.75, 1.0]),
        "news_count": float(min(12, int(rng.expovariate(1 / 3.0)))),
        "adx_1h": max(3.0, rng.gauss(23.0, 9.0)),
        "rating_4h": rng.uniform(-1.0, 1.0),
        "rating_1d": rng.uniform(-1.0, 1.0),
    }


def build_calls(
    count: int, rng: random.Random, bad_cell_rate: float | None = None
) -> list[learning.EvaluatedCall]:
    """採点済み判断を合成する。bad_cell_rate 指定時だけ1セルに真の劣位を置く。"""
    calls: list[learning.EvaluatedCall] = []
    for index in range(count):
        features = sample_features(rng)
        direction = rng.choice(["long", "short"])
        rate = 0.5
        if bad_cell_rate is not None and features["rsi_1h"] < 35.0 and direction == "long":
            rate = bad_cell_rate
        calls.append(
            learning.EvaluatedCall(
                symbol="USDJPY",
                direction=direction,
                conviction=rng.randint(20, 80),
                tech_score=rng.uniform(-1.0, 1.0),
                news_score=rng.uniform(-1.0, 1.0),
                outcome="hit" if rng.random() < rate else "miss",
                ts=(SLICE_START + timedelta(hours=index)).isoformat(),
                features=features,
            )
        )
    return calls


def count_cells(factors: Mapping[str, Mapping[str, Mapping[str, float]]]) -> int:
    return sum(len(directions) for buckets in factors.values() for directions in buckets.values())


def run_null(count: int) -> dict[str, object]:
    """コイン投げの入力に対して生成される減衰セルと重み変動を数える。"""
    cells: list[int] = []
    decayed_share: list[float] = []
    weights: list[float] = []
    moved = 0
    for seed in range(SEEDS):
        rng = random.Random(1000 + seed)
        profile = learning.derive_profile(build_calls(count, rng))
        cells.append(count_cells(profile.condition_factors))
        weights.append(profile.tech_weight)
        moved += abs(profile.tech_weight - TECH_WEIGHT) > 1e-9
        decayed = sum(
            profile.condition_adjustment(sample_features(rng), rng.choice(["long", "short"]))[0]
            < 1.0
            for _ in range(PROBES)
        )
        decayed_share.append(decayed / PROBES)
    return {
        "n_scored_per_slice": count,
        "market_weeks_at_one_per_hour": round(count / MARKET_HOURS_PER_WEEK, 1),
        "false_decay_cells_mean": round(statistics.mean(cells), 2),
        "false_decay_cells_max": max(cells),
        "total_cells": len(learning.FEATURE_SPECS) * 3 * 2,
        "share_of_new_decisions_decayed": round(statistics.mean(decayed_share), 4),
        "tech_weight_default": TECH_WEIGHT,
        "tech_weight_mean": round(statistics.mean(weights), 4),
        "tech_weight_sd": round(statistics.pstdev(weights), 4),
        "tech_weight_min": min(weights),
        "tech_weight_max": max(weights),
        "share_seeds_weight_moved": round(moved / SEEDS, 3),
    }


def run_power(count: int, bad_cell_rate: float) -> dict[str, object]:
    """真に劣位な1セルの検出率と、同時に出る偽陽性セル数を測る。"""
    detected = 0
    false_cells: list[int] = []
    true_cell_samples: list[int] = []
    for seed in range(SEEDS):
        calls = build_calls(count, random.Random(7000 + seed), bad_cell_rate)
        profile = learning.derive_profile(calls)
        factors = profile.condition_factors
        hit = "long" in factors.get("rsi_1h", {}).get(TRUE_BAD_BUCKET, {})
        detected += hit
        false_cells.append(count_cells(factors) - (1 if hit else 0))
        true_cell_samples.append(
            sum(
                1
                for call in learning.thin_calls(calls, learning.DERIVE_THIN_GAP_HOURS)
                if call.direction == "long" and call.features["rsi_1h"] < 35.0
            )
        )
    detection_rate = detected / SEEDS
    false_mean = statistics.mean(false_cells)
    precision = detection_rate / (detection_rate + false_mean) if detection_rate + false_mean else 0
    return {
        "n_scored_per_slice": count,
        "true_bad_cell_hit_rate": bad_cell_rate,
        "true_cell_samples_mean": round(statistics.mean(true_cell_samples), 1),
        "detection_rate": round(detection_rate, 3),
        "false_cells_mean": round(false_mean, 2),
        "precision": round(precision, 4),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("null", "power", "both"), default="both")
    args = parser.parse_args()

    payload: dict[str, object] = {
        "generated_by": "reports/research/learning-null-power-20260823/null_power_experiment.py",
        "seeds": SEEDS,
        "probes_per_seed": PROBES,
        "constants": {
            "MIN_CONDITION_SAMPLES": learning.MIN_CONDITION_SAMPLES,
            "CONDITION_HIT_RATE_TRIGGER": learning.CONDITION_HIT_RATE_TRIGGER,
            "CONDITION_FACTOR_MIN": learning.CONDITION_FACTOR_MIN,
            "MIN_WEIGHT_SAMPLES": learning.MIN_WEIGHT_SAMPLES,
            "WEIGHT_SHRINK_HALFWAY": learning.WEIGHT_SHRINK_HALFWAY,
            "TECH_WEIGHT_MIN": learning.TECH_WEIGHT_MIN,
            "TECH_WEIGHT_MAX": learning.TECH_WEIGHT_MAX,
            "DERIVE_THIN_GAP_HOURS": learning.DERIVE_THIN_GAP_HOURS,
        },
    }
    if args.mode in ("null", "both"):
        payload["null"] = [run_null(count) for count in SAMPLE_SIZES]
    if args.mode in ("power", "both"):
        payload["power"] = [
            run_power(count, rate)
            for rate in (0.40, 0.45)
            for count in SAMPLE_SIZES
            if count >= 500
        ]
    print(json.dumps(payload, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
