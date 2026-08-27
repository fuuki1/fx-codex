"""P1(履歴prior)の実現可能性を、リポジトリ内の実データだけで測る。

`fx_intel/learning.py` の状態×方向セル学習は、セルごとの的中率が全体から
どれだけ乖離しているかを推定して確信度を減衰させる。この診断は「その乖離
(セル間分散 tau)が、実データでどれだけ存在するのか」を上限付きで測る。

測るもの:

1. 履歴リプレイで得られる標本量 — 生の判断点数と、重複しないラベル窓の数。
   1時間足バーに対して24時間ラベルを貼ると、隣接標本は23/24の未来を共有する。
   実効標本は 生標本 / (ホライズン/バー間隔) であり、この補正を落とすと
   有意性を桁で誤る。
2. 無条件バイアス(トレンド焼き込み) — (ペア×方向)ごとの素の的中率。
   2024年のように一方向へ動いた年は、この差がそのまま「セルの優位」に
   化けるため、prior化する前に必ず差し引く必要がある。
3. セル間分散 tau — DerSimonian-Laird 推定量(メタ分析の標準)で、
   各セルの標本誤差を除いた「真の乖離のばらつき」を推定し、片側95%上限を出す。
4. 推定量の陽性対照 — 既知の tau を注入して回収できるかを確認する。
   これが通らない限り「tau=0」という結論は信用できない。

特徴量は OHLC から再計算できる4つ(RSI/ADX/MA乖離/ATR%)に限る。
TradingViewレーティング由来(rating_4h / rating_1d / tf_agreement)と
ニュース件数は履歴再現の対象外であり、本診断の結論はそれらを含まない。

実行:

    .venv/bin/python reports/research/p1-historical-prior-feasibility-20260823/feasibility_probe.py

追加依存なし(標準ライブラリのみ)。ネットワークアクセスなし。
`logs/` を含む runtime data には一切書き込まない。
"""

from __future__ import annotations

import csv
import json
import math
import random
from collections import defaultdict
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
PAIRS = {
    "USDJPY": "data/real/histdata/usdjpy_2024_1h.csv",
    "EURUSD": "data/real/histdata/eurusd_2024_1h.csv",
    "GBPUSD": "data/real/histdata/gbpusd_2024_1h.csv",
}
# バー間隔=1時間なので、ホライズン時間 = 重複係数 = 何本先を見るか。
HORIZON_BARS = {"1h": 1, "4h": 4, "24h": 24}
FLAT_ATR_FRACTION = 0.1  # learning.DEFAULT_ATR_FRACTION と同じ
FEATURES = ("rsi_1h", "adx_1h", "ma_gap_atr", "atr_pct")
# learning.FEATURE_SPECS と同じ境界(表示名のみ簡略化)
BUCKETS: dict[str, tuple[tuple[str, float, float], ...]] = {
    "rsi_1h": (("oversold", -1e9, 35.0), ("neutral", 35.0, 65.0), ("overbought", 65.0, 1e9)),
    "ma_gap_atr": (("small", 0.0, 0.5), ("mid", 0.5, 2.0), ("large", 2.0, 1e9)),
    "atr_pct": (("low_vol", 0.0, 0.10), ("mid_vol", 0.10, 0.25), ("high_vol", 0.25, 1e9)),
    "adx_1h": (("range", 0.0, 20.0), ("weak_trend", 20.0, 30.0), ("strong_trend", 30.0, 1e9)),
}
ABS_FEATURES = {"ma_gap_atr"}
# chi2(df) の下側5%点。tau^2 の保守的な片側95%上限に使う。
CHI2_LOWER_05 = {5: 1.145, 23: 13.091}


def load_bars(path: Path) -> list[tuple[datetime, float, float, float, float]]:
    rows: list[tuple[datetime, float, float, float, float]] = []
    with path.open() as handle:
        for row in csv.DictReader(handle):
            rows.append(
                (
                    datetime.fromisoformat(row["timestamp"]),
                    float(row["open"]),
                    float(row["high"]),
                    float(row["low"]),
                    float(row["close"]),
                )
            )
    rows.sort(key=lambda item: item[0])
    return rows


def wilder(values: list[float], period: int) -> list[float | None]:
    """Wilder平滑(fx_backtester/indicators.py の ewm(alpha=1/period) と同一)。"""
    out: list[float | None] = [None] * len(values)
    if len(values) < period:
        return out
    acc = sum(values[:period]) / period
    out[period - 1] = acc
    for index in range(period, len(values)):
        acc = (acc * (period - 1) + values[index]) / period
        out[index] = acc
    return out


def rsi_series(closes: list[float], period: int = 14) -> list[float | None]:
    gains = [max(closes[i] - closes[i - 1], 0.0) for i in range(1, len(closes))]
    losses = [max(closes[i - 1] - closes[i], 0.0) for i in range(1, len(closes))]
    avg_gain, avg_loss = wilder(gains, period), wilder(losses, period)
    out: list[float | None] = [None] * len(closes)
    for index, gain in enumerate(avg_gain):
        loss = avg_loss[index]
        if gain is None or loss is None:
            continue
        out[index + 1] = 100.0 if loss == 0 else 100.0 - 100.0 / (1.0 + gain / loss)
    return out


def atr_adx_series(
    rows: list[tuple[datetime, float, float, float, float]], period: int = 14
) -> tuple[list[float | None], list[float | None]]:
    total = len(rows)
    true_range: list[float] = []
    plus_dm: list[float] = []
    minus_dm: list[float] = []
    for index in range(1, total):
        _, _, high, low, _ = rows[index]
        prev_high, prev_low, prev_close = rows[index - 1][2], rows[index - 1][3], rows[index - 1][4]
        true_range.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
        up, down = high - prev_high, prev_low - low
        plus_dm.append(up if (up > down and up > 0) else 0.0)
        minus_dm.append(down if (down > up and down > 0) else 0.0)
    atr_s = wilder(true_range, period)
    pdm_s = wilder(plus_dm, period)
    mdm_s = wilder(minus_dm, period)
    atr: list[float | None] = [None] * total
    dx: list[float | None] = [None] * total
    for index, atr_value in enumerate(atr_s):
        pdm, mdm = pdm_s[index], mdm_s[index]
        if atr_value is None or atr_value == 0 or pdm is None or mdm is None:
            continue
        atr[index + 1] = atr_value
        plus_di = 100.0 * pdm / atr_value
        minus_di = 100.0 * mdm / atr_value
        spread = plus_di + minus_di
        dx[index + 1] = 0.0 if spread == 0 else 100.0 * abs(plus_di - minus_di) / spread
    dx_values = [value for value in dx if value is not None]
    adx_smoothed = wilder(dx_values, period)
    adx: list[float | None] = [None] * total
    first = next((i for i, value in enumerate(dx) if value is not None), None)
    if first is not None:
        for offset, value in enumerate(adx_smoothed):
            if value is not None and first + offset < total:
                adx[first + offset] = value
    return atr, adx


def sma_series(closes: list[float], window: int) -> list[float | None]:
    out: list[float | None] = [None] * len(closes)
    running = 0.0
    for index, close in enumerate(closes):
        running += close
        if index >= window:
            running -= closes[index - window]
        if index >= window - 1:
            out[index] = running / window
    return out


def bucket_of(feature: str, value: float) -> str | None:
    target = abs(value) if feature in ABS_FEATURES else value
    for label, low, high in BUCKETS[feature]:
        if low <= target < high:
            return label
    return None


def build_points() -> list[dict[str, object]]:
    """全ペアの (時刻, 特徴量, 将来終値列) を作る。特徴量は t 以前のバーのみ使う。"""
    points: list[dict[str, object]] = []
    for pair, relative in PAIRS.items():
        rows = load_bars(REPO_ROOT / relative)
        closes = [row[4] for row in rows]
        rsi = rsi_series(closes)
        atr, adx = atr_adx_series(rows)
        fast, slow = sma_series(closes, 20), sma_series(closes, 100)
        for index in range(len(rows)):
            atr_value = atr[index]
            if None in (rsi[index], adx[index], fast[index], slow[index]) or not atr_value:
                continue
            assert rsi[index] is not None and adx[index] is not None
            assert fast[index] is not None and slow[index] is not None
            points.append(
                {
                    "pair": pair,
                    "index": index,
                    "ts": rows[index][0],
                    "close": closes[index],
                    "atr": atr_value,
                    "features": {
                        "rsi_1h": float(rsi[index]),  # type: ignore[arg-type]
                        "adx_1h": float(adx[index]),  # type: ignore[arg-type]
                        "ma_gap_atr": (float(fast[index]) - float(slow[index]))  # type: ignore[arg-type]
                        / atr_value,
                        "atr_pct": atr_value / closes[index] * 100.0,
                    },
                    "closes": closes,
                }
            )
    return points


def label(
    points: list[dict[str, object]], step: int
) -> list[tuple[str, str, datetime, dict, bool]]:
    """両方向にラベルを貼る。ATR×0.1未満の値動きは production と同じくflatで除外。"""
    out: list[tuple[str, str, datetime, dict, bool]] = []
    for point in points:
        closes: list[float] = point["closes"]  # type: ignore[assignment]
        index: int = point["index"]  # type: ignore[assignment]
        if index + step >= len(closes):
            continue
        move = closes[index + step] - float(point["close"])  # type: ignore[arg-type]
        threshold = FLAT_ATR_FRACTION * float(point["atr"])  # type: ignore[arg-type]
        for direction in ("long", "short"):
            signed = move if direction == "long" else -move
            if abs(signed) <= threshold:
                continue
            out.append(
                (
                    str(point["pair"]),
                    direction,
                    point["ts"],  # type: ignore[arg-type]
                    point["features"],  # type: ignore[arg-type]
                    signed > threshold,
                )
            )
    return out


def cell_deviations(
    observations: list[tuple[str, str, datetime, dict, bool]], step: int, feature: str | None = None
) -> tuple[list[float], list[float], list[float], dict[str, float]]:
    """(ペア×方向)の無条件的中率を差し引いたセル偏差と、その実効分散を返す。"""
    baseline: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])
    for pair, direction, _ts, _features, hit in observations:
        stats = baseline[(pair, direction)]
        stats[0] += 1
        stats[1] += int(hit)
    cells: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0, 0.0])
    for pair, direction, _ts, features, hit in observations:
        stats = baseline[(pair, direction)]
        base_rate = stats[1] / stats[0]
        for key, value in features.items():
            if feature is not None and key != feature:
                continue
            label_name = bucket_of(key, value)
            if label_name is None:
                continue
            cell = cells[f"{key}|{label_name}|{direction}"]
            cell[0] += 1.0
            cell[1] += float(hit)
            cell[2] += base_rate
    deviations: list[float] = []
    variances: list[float] = []
    effective: list[float] = []
    for _cell, (count, hits, base_sum) in sorted(cells.items()):
        effective_n = count / step
        base_rate = base_sum / count
        deviations.append(hits / count - base_rate)
        variances.append(max(base_rate * (1.0 - base_rate), 1e-6) / effective_n)
        effective.append(effective_n)
    unconditional = {
        f"{pair}|{direction}": round(stats[1] / stats[0], 4)
        for (pair, direction), stats in sorted(baseline.items())
    }
    return deviations, variances, effective, unconditional


def dersimonian_laird(deviations: list[float], variances: list[float]) -> dict[str, object]:
    """メタ分析標準の tau^2 推定。標本誤差を除いた真のばらつきを推定する。"""
    count = len(deviations)
    weights = [1.0 / variance for variance in variances]
    weight_sum = sum(weights)
    weighted_mean = sum(w * d for w, d in zip(weights, deviations)) / weight_sum
    q_stat = sum(w * (d - weighted_mean) ** 2 for w, d in zip(weights, deviations))
    scale = weight_sum - sum(w * w for w in weights) / weight_sum
    tau2 = max(0.0, (q_stat - (count - 1)) / scale) if scale > 0 else 0.0
    chi2 = CHI2_LOWER_05.get(count - 1)
    tau2_upper = max(0.0, (q_stat - chi2) / scale) if chi2 is not None and scale > 0 else None
    return {
        "cells": count,
        "Q": round(q_stat, 2),
        "Q_df": count - 1,
        "tau_hat": round(math.sqrt(tau2), 5),
        "tau_upper95": round(math.sqrt(tau2_upper), 5) if tau2_upper is not None else None,
        "max_abs_deviation": round(max(abs(d) for d in deviations), 4),
    }


def holm_survivors(deviations: list[float], variances: list[float], alpha: float = 0.05) -> int:
    """偏差ゼロの帰無仮説に対するHolm補正後の生存セル数。"""
    p_values = sorted(
        math.erfc(abs(d) / math.sqrt(v) / math.sqrt(2)) for d, v in zip(deviations, variances)
    )
    total = len(p_values)
    survivors = 0
    for rank, p_value in enumerate(p_values):
        if p_value <= alpha / (total - rank):
            survivors += 1
        else:
            break
    return survivors


def cells_without_demean(
    observations: list[tuple[str, str, datetime, dict, bool]], step: int
) -> tuple[list[float], list[float]]:
    """de-meaningを外した場合のセル偏差(基準を一律0.5に置く)。"""
    cells: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
    for _pair, direction, _ts, features, hit in observations:
        for key, value in features.items():
            label_name = bucket_of(key, value)
            if label_name is None:
                continue
            cell = cells[f"{key}|{label_name}|{direction}"]
            cell[0] += 1.0
            cell[1] += float(hit)
    deviations = [hits / count - 0.5 for count, hits in (v for _k, v in sorted(cells.items()))]
    variances = [0.25 / (count / step) for count, _hits in (v for _k, v in sorted(cells.items()))]
    return deviations, variances


def positive_control(tau_true: float, seeds: int = 10, points: int = 18000, step: int = 4) -> dict:
    """既知の tau を注入して推定量が回収できるかを確認する。"""
    estimates: list[float] = []
    for seed in range(seeds):
        rng = random.Random(555 + seed)
        offsets = {cell: rng.gauss(0.0, tau_true) for cell in range(6)}
        counts = [[0, 0] for _ in range(6)]
        for _ in range(points):
            cell = rng.choices(range(6), weights=[0.25, 0.55, 0.20, 0.25, 0.55, 0.20])[0]
            rate = min(0.95, max(0.05, 0.5 + offsets[cell]))
            counts[cell][0] += 1
            counts[cell][1] += int(rng.random() < rate)
        deviations = [counts[c][1] / counts[c][0] - 0.5 for c in range(6)]
        variances = [0.25 / (counts[c][0] / step) for c in range(6)]
        estimates.append(float(dersimonian_laird(deviations, variances)["tau_hat"]))
    return {
        "tau_true": tau_true,
        "tau_hat_mean": round(sum(estimates) / len(estimates), 5),
        "tau_hat_min": round(min(estimates), 5),
        "tau_hat_max": round(max(estimates), 5),
    }


def main() -> None:
    points = build_points()
    report: dict[str, object] = {
        "generated_by": (
            "reports/research/p1-historical-prior-feasibility-20260823/feasibility_probe.py"
        ),
        "data": {
            pair: {
                "rows": sum(1 for p in points if p["pair"] == pair),
                "path": path,
            }
            for pair, path in PAIRS.items()
        },
        "positive_control": [positive_control(t) for t in (0.0, 0.01, 0.02, 0.03, 0.05)],
    }
    horizons: dict[str, object] = {}
    for horizon, step in HORIZON_BARS.items():
        observations = label(points, step)
        deviations, variances, effective, unconditional = cell_deviations(observations, step)
        entry: dict[str, object] = {
            "labeled_observations": len(observations),
            "decision_points": len(observations) // 2,
            "non_overlapping_windows": len(observations) // (2 * step),
            "median_effective_n_per_cell": round(sorted(effective)[len(effective) // 2], 1),
            "unconditional_hit_rate": unconditional,
            "unconditional_spread": round(
                max(unconditional.values()) - min(unconditional.values()), 4
            ),
            "all_features": dersimonian_laird(deviations, variances),
        }
        for feature in FEATURES:
            f_dev, f_var, _f_eff, _ = cell_deviations(observations, step, feature)
            entry[feature] = dersimonian_laird(f_dev, f_var)
        raw_variances = [variance / step for variance in variances]  # 重複補正を外した分散
        entry["overlap_correction"] = {
            "holm_survivors_raw_n": holm_survivors(deviations, raw_variances),
            "holm_survivors_effective_n": holm_survivors(deviations, variances),
        }
        nd_dev, nd_var = cells_without_demean(observations, step)
        entry["demean_comparison"] = {
            "with_demean": dersimonian_laird(deviations, variances),
            "without_demean": dersimonian_laird(nd_dev, nd_var),
        }
        horizons[horizon] = entry
    report["horizons"] = horizons
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
