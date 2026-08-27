#!/usr/bin/env python3
"""履歴バーからセル間分散の上限を測り、学習priorのartifactを生成する。

`fx_intel/learning.py` の状態×方向セル学習は、セルごとの的中率をそのセルの
標本だけで推定する(= セル間の真の分散を無限大と仮定する)。本ツールは履歴バーで
その分散の**片側95%上限**を測り、`config/learning_prior_v1.json` へ固定する。
学習ループはこの上限でセル率推定を収縮し、上限に見合わない大きな調整をやめる。

設計と測定根拠:

- docs/design/P1_HISTORICAL_PRIOR.md
- reports/research/p1-historical-prior-feasibility-20260823/

測るもの(スライス別):

    fusion   1h足の特徴量 × 24時間先ラベル   (融合判断と同じ組み合わせ)
    tf:15m   15m足の特徴量 × 15分先ラベル    (--corpus m5 のときだけ)
    tf:1h    1h足の特徴量 × 1時間先ラベル    (時間足別判断と同じ組み合わせ)
    tf:4h    4h足の特徴量 × 4時間先ラベル
    tf:1d    1d足の特徴量 × 24時間先ラベル

コーパスは2種類。

    --corpus csv1h (既定)  data/real/histdata/*_2024_1h.csv (コミット済み)
    --corpus m5            data/historical_training/histdata/bars_m5 (実機のみ)

`tf:15m` は1時間足を分割できないため csv1h では生成しない(未測定として扱われ、
学習ループはその時間足だけ従来挙動のままになる)。M5コーパスがある環境で
`--corpus m5` を使えば埋まる。

必須の前処理は2つあり、どちらも欠かすと結論が壊れる。

1. **(ペア×方向)のde-mean** — その期間の相場方向がセルの優位に化けるのを防ぐ。
   2024年24hのUSDJPYは long 58.0% / short 42.0% で、この差だけで減衰トリガーを跨ぐ。
2. **実効nへの補正** — 1時間足に24時間ラベルを貼ると隣接標本が未来の23/24を共有する。
   生の件数で分散を測ると24倍過小評価する。

実行:

    .venv/bin/python tools/build_learning_prior.py --output config/learning_prior_v1.json

`--dry-run` で既存artifactとの差分だけ表示する。追加依存なし・ネットワーク無し・
runtime data(`logs/`)へは一切書き込まない。
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import math
import sys
from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime, timedelta, UTC
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fx_intel import learning_prior  # noqa: E402
from fx_intel.learning import (  # noqa: E402
    CONDITION_HIT_RATE_TRIGGER,
    FEATURE_SPECS,
    bucket_for,
)

DEFAULT_OUTPUT = REPO_ROOT / "config" / "learning_prior_v1.json"
DEFAULT_CORPUS_ID = "histdata-1h-2024-3pairs"
DEFAULT_SOURCES: tuple[tuple[str, str], ...] = (
    ("USDJPY", "data/real/histdata/usdjpy_2024_1h.csv"),
    ("EURUSD", "data/real/histdata/eurusd_2024_1h.csv"),
    ("GBPUSD", "data/real/histdata/gbpusd_2024_1h.csv"),
)
# M5 bid/askコーパス。fx_intel/historical_chart.py が使う配置と同じものを読む
# (<root>/<PAIR>/<PAIR>_*_m5_bidask.csv.gz、mid = (bid+ask)/2)。
DEFAULT_M5_ROOT = REPO_ROOT / "data" / "historical_training" / "histdata" / "bars_m5"
DEFAULT_M5_PAIRS: tuple[str, ...] = ("USDJPY", "EURUSD", "GBPUSD")
DEFAULT_M5_CORPUS_ID = "histdata-m5-bidask"
M5_LICENSE_NOTE = (
    "HistData.com M1 -> M5 bid/ask bars. Personal/research use per HistData terms. "
    "NOT admissible for any promotion or performance claim."
)
LICENSE_NOTE = (
    "HistData.com free ASCII M1, EST->UTC, resampled to 1h. Personal/research use per "
    "HistData terms. CLOSE-ONLY (no bid/ask, no real volume): NOT admissible for any "
    "promotion or performance claim."
)
# OHLCから再計算できる特徴量だけを対象にする。TradingViewレーティング由来
# (rating_4h / rating_1d / tf_agreement)と news_count は履歴再現できない。
REPLAYABLE_FEATURES = ("rsi_1h", "adx_1h", "ma_gap_atr", "atr_pct")
FLAT_ATR_FRACTION = 0.1  # learning.DEFAULT_ATR_FRACTION と同じ小動き除外幅
INDICATOR_PERIOD = 14
MA_FAST, MA_SLOW = 20, 100  # technicals.PairTechnicals の fast/slow と同じ
# FX日の境界(17:00 EST = 22:00 UTC)。4h/1d足のバケット境界をここへ揃える。
FX_DAY_ANCHOR = datetime(2024, 1, 1, 22, 0, tzinfo=UTC)
BAR_ALIGNMENT = "fx_day_22utc"
# ラベル先バーとの許容ウォールクロック差。historical_chart.MAX_LABEL_GAPS と同じ思想。
MAX_LABEL_GAP_HOURS = {"15m": 0.75, "1h": 3.0, "4h": 12.0, "1d": 72.0}
# スライス定義: (特徴量を計算するバーの時間足, ラベルまでのバー数)。
# 15mはM5コーパスでしか作れない(1時間足からは分割できない)。
SLICES_1H: tuple[tuple[str, str, int], ...] = (
    (learning_prior.FUSION_KEY, "1h", 24),
    (learning_prior.timeframe_key("1h"), "1h", 1),
    (learning_prior.timeframe_key("4h"), "4h", 1),
    (learning_prior.timeframe_key("1d"), "1d", 1),
)
SLICES_M5: tuple[tuple[str, str, int], ...] = (
    (learning_prior.FUSION_KEY, "1h", 24),
    (learning_prior.timeframe_key("15m"), "15m", 1),
    *SLICES_1H[1:],
)
TIMEFRAME_HOURS = {"15m": 0.25, "1h": 1.0, "4h": 4.0, "1d": 24.0}

Bar = tuple[datetime, float, float, float, float]


def load_bars(path: Path) -> list[Bar]:
    rows: list[Bar] = []
    with path.open(encoding="utf-8") as handle:
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


def load_m5_bars(root: Path, pair: str) -> list[Bar]:
    """M5 bid/askコーパスを mid OHLC として読む(historical_chart と同じ配置)。"""
    paths = sorted((root / pair).glob(f"{pair}_*_m5_bidask.csv.gz"))
    if not paths:
        raise FileNotFoundError(f"historical bid/ask files not found: {root / pair}")
    rows: dict[datetime, Bar] = {}
    for path in paths:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                stamp = datetime.fromisoformat(row["timestamp"])
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=UTC)
                mid = tuple(
                    (float(row[f"bid_{field}"]) + float(row[f"ask_{field}"])) / 2.0
                    for field in ("open", "high", "low", "close")
                )
                rows[stamp] = (stamp, mid[0], mid[1], mid[2], mid[3])
    return [rows[stamp] for stamp in sorted(rows)]


def resample(bars: Sequence[Bar], hours: float, source_hours: float = 1.0) -> list[Bar]:
    """FX日境界に揃えて上位足へ集約する。欠損バケットは作らない。"""
    if hours <= source_hours:
        return list(bars)
    buckets: dict[int, list[Bar]] = defaultdict(list)
    for bar in bars:
        offset = (bar[0] - FX_DAY_ANCHOR).total_seconds() / 3600.0
        buckets[math.floor(offset / hours)].append(bar)
    out: list[Bar] = []
    for index in sorted(buckets):
        group = buckets[index]
        out.append(
            (
                FX_DAY_ANCHOR + timedelta(hours=index * hours),
                group[0][1],
                max(item[2] for item in group),
                min(item[3] for item in group),
                group[-1][4],
            )
        )
    return out


def _wilder(values: Sequence[float], period: int) -> list[float | None]:
    """Wilder平滑。fx_backtester/indicators.py の ewm(alpha=1/period) と一致する。"""
    out: list[float | None] = [None] * len(values)
    if len(values) < period:
        return out
    average = sum(values[:period]) / period
    out[period - 1] = average
    for index in range(period, len(values)):
        average = (average * (period - 1) + values[index]) / period
        out[index] = average
    return out


def _rsi(closes: Sequence[float], period: int = INDICATOR_PERIOD) -> list[float | None]:
    gains = [max(closes[i] - closes[i - 1], 0.0) for i in range(1, len(closes))]
    losses = [max(closes[i - 1] - closes[i], 0.0) for i in range(1, len(closes))]
    average_gain, average_loss = _wilder(gains, period), _wilder(losses, period)
    out: list[float | None] = [None] * len(closes)
    for index, gain in enumerate(average_gain):
        loss = average_loss[index]
        if gain is None or loss is None:
            continue
        out[index + 1] = 100.0 if loss == 0 else 100.0 - 100.0 / (1.0 + gain / loss)
    return out


def _atr_adx(
    bars: Sequence[Bar], period: int = INDICATOR_PERIOD
) -> tuple[list[float | None], list[float | None]]:
    total = len(bars)
    true_range: list[float] = []
    plus_dm: list[float] = []
    minus_dm: list[float] = []
    for index in range(1, total):
        _, _, high, low, _ = bars[index]
        previous = bars[index - 1]
        true_range.append(
            max(high - low, abs(high - previous[4]), abs(low - previous[4])),
        )
        up, down = high - previous[2], previous[3] - low
        plus_dm.append(up if (up > down and up > 0) else 0.0)
        minus_dm.append(down if (down > up and down > 0) else 0.0)
    atr_smoothed = _wilder(true_range, period)
    plus_smoothed = _wilder(plus_dm, period)
    minus_smoothed = _wilder(minus_dm, period)
    atr: list[float | None] = [None] * total
    directional_index: list[float | None] = [None] * total
    for index, value in enumerate(atr_smoothed):
        plus, minus = plus_smoothed[index], minus_smoothed[index]
        if not value or plus is None or minus is None:
            continue
        atr[index + 1] = value
        plus_di, minus_di = 100.0 * plus / value, 100.0 * minus / value
        total_di = plus_di + minus_di
        directional_index[index + 1] = (
            0.0 if total_di == 0 else 100.0 * abs(plus_di - minus_di) / total_di
        )
    present = [value for value in directional_index if value is not None]
    smoothed = _wilder(present, period)
    adx: list[float | None] = [None] * total
    first = next((i for i, value in enumerate(directional_index) if value is not None), None)
    if first is not None:
        for offset, value in enumerate(smoothed):
            if value is not None and first + offset < total:
                adx[first + offset] = value
    return atr, adx


def _sma(closes: Sequence[float], window: int) -> list[float | None]:
    out: list[float | None] = [None] * len(closes)
    running = 0.0
    for index, close in enumerate(closes):
        running += close
        if index >= window:
            running -= closes[index - window]
        if index >= window - 1:
            out[index] = running / window
    return out


def observations(
    bars: Sequence[Bar], timeframe: str, horizon_bars: int
) -> list[tuple[str, dict[str, float], bool]]:
    """1ペアぶんの (方向, 特徴量, hit) 列。特徴量は t 以前のバーだけから作る。"""
    closes = [bar[4] for bar in bars]
    rsi = _rsi(closes)
    atr, adx = _atr_adx(bars)
    fast, slow = _sma(closes, MA_FAST), _sma(closes, MA_SLOW)
    max_gap = MAX_LABEL_GAP_HOURS.get(timeframe)
    nominal = TIMEFRAME_HOURS[timeframe] * horizon_bars
    out: list[tuple[str, dict[str, float], bool]] = []
    for index in range(len(bars) - horizon_bars):
        atr_value = atr[index]
        rsi_value, adx_value = rsi[index], adx[index]
        fast_value, slow_value = fast[index], slow[index]
        if not atr_value or rsi_value is None or adx_value is None:
            continue
        if fast_value is None or slow_value is None:
            continue
        target = bars[index + horizon_bars]
        gap_hours = (target[0] - bars[index][0]).total_seconds() / 3600.0
        if max_gap is not None and horizon_bars == 1 and gap_hours > max_gap:
            continue
        if horizon_bars > 1 and gap_hours > nominal + max(MAX_LABEL_GAP_HOURS.values()):
            continue
        features = {
            "rsi_1h": rsi_value,
            "adx_1h": adx_value,
            "ma_gap_atr": (fast_value - slow_value) / atr_value,
            "atr_pct": atr_value / closes[index] * 100.0,
        }
        move = target[4] - closes[index]
        threshold = FLAT_ATR_FRACTION * atr_value
        for direction in ("long", "short"):
            signed = move if direction == "long" else -move
            if abs(signed) <= threshold:
                continue  # flat: production と同じく判定除外
            out.append((direction, features, signed > threshold))
    return out


def bucketize(feature: str, value: float) -> str | None:
    """learning.FEATURE_SPECS のバケット名を返す(共有ルーチンへ渡す判定器)。"""
    spec = next((item for item in FEATURE_SPECS if item.key == feature), None)
    if spec is None:
        return None
    bucket = bucket_for(spec, value)
    return bucket.label_ja if bucket is not None else None


def measure_slice(
    per_pair: dict[str, list[tuple[str, dict[str, float], bool]]], overlap: float
) -> dict[str, dict[str, object]]:
    """(ペア×方向)でde-meanし、実効nでセル間分散を測る。

    運用ジャーナル側(tools/learning_prior_audit.py)と同じ
    learning_prior.cell_deviations を使う。両者の tau が同じ定義で比較できることが、
    転移の検証(G7)が成立する前提になる。
    """
    observations = [
        (pair, direction, features, hit)
        for pair, rows in sorted(per_pair.items())
        for direction, features, hit in rows
    ]
    measured: dict[str, dict[str, object]] = {}
    for feature, cells in learning_prior.cell_deviations(
        observations, overlap, REPLAYABLE_FEATURES, bucketize
    ).items():
        if len(cells.deviations) < 2:
            continue
        tau_hat, tau_upper, q_statistic = learning_prior.dersimonian_laird(
            cells.deviations, cells.variances
        )
        effective = sorted(cells.effective)
        measured[feature] = {
            "tau_upper95": round(tau_upper, 6),
            "tau_hat": round(tau_hat, 6),
            "cells": len(cells.deviations),
            "Q": round(q_statistic, 4),
            "n_eff_median": round(effective[len(effective) // 2], 1),
            "parity": learning_prior.PARITY_UNVERIFIED,
            "max_abs_deviation": round(max(abs(value) for value in cells.deviations), 6),
            # cells_detail は監査・将来利用のみ。学習ループは読まない。
            "cells_detail": {
                name: {"deviation": values["deviation"], "n_eff": values["n_eff"]}
                for name, values in cells.detail.items()
            },
            "advisory_only": ["cells_detail", "max_abs_deviation"],
        }
    return measured


def _load_csv1h_corpus(
    sources: Sequence[tuple[str, str]],
) -> tuple[dict[str, list[Bar]], list[dict[str, object]]]:
    bars_by_pair: dict[str, list[Bar]] = {}
    datasets: list[dict[str, object]] = []
    for pair, relative in sources:
        path = REPO_ROOT / relative
        raw = path.read_bytes()
        bars_by_pair[pair] = load_bars(path)
        datasets.append(
            {
                "pair": pair,
                "path": relative,
                "sha256": hashlib.sha256(raw).hexdigest(),
                "bars": len(bars_by_pair[pair]),
                "license_note": LICENSE_NOTE,
            }
        )
    return bars_by_pair, datasets


def _load_m5_corpus(
    root: Path, pairs: Sequence[str]
) -> tuple[dict[str, list[Bar]], list[dict[str, object]]]:
    """M5コーパスは月次ファイル群なので、ファイル単位のhashをまとめて記録する。"""
    bars_by_pair: dict[str, list[Bar]] = {}
    datasets: list[dict[str, object]] = []
    for pair in pairs:
        bars_by_pair[pair] = load_m5_bars(root, pair)
        files = sorted((root / pair).glob(f"{pair}_*_m5_bidask.csv.gz"))
        digest = hashlib.sha256()
        for path in files:
            digest.update(hashlib.sha256(path.read_bytes()).digest())
        datasets.append(
            {
                "pair": pair,
                "path": str(root / pair),
                "sha256": digest.hexdigest(),
                "files": len(files),
                "bars": len(bars_by_pair[pair]),
                "license_note": M5_LICENSE_NOTE,
            }
        )
    return bars_by_pair, datasets


def build_payload(
    sources: Sequence[tuple[str, str]],
    corpus_id: str,
    *,
    corpus: str = "csv1h",
    m5_root: Path | None = None,
    m5_pairs: Sequence[str] = DEFAULT_M5_PAIRS,
) -> dict[str, object]:
    if corpus == "m5":
        bars_by_pair, datasets = _load_m5_corpus(m5_root or DEFAULT_M5_ROOT, m5_pairs)
        source_hours, slice_specs = 5.0 / 60.0, SLICES_M5
    else:
        bars_by_pair, datasets = _load_csv1h_corpus(sources)
        source_hours, slice_specs = 1.0, SLICES_1H
    stamps = [bar[0] for bars in bars_by_pair.values() for bar in bars]
    slices: dict[str, dict[str, object]] = {}
    for slice_key, timeframe, horizon_bars in slice_specs:
        per_pair = {
            pair: observations(
                resample(bars, TIMEFRAME_HOURS[timeframe], source_hours), timeframe, horizon_bars
            )
            for pair, bars in bars_by_pair.items()
        }
        if not any(per_pair.values()):
            continue
        measured = measure_slice(per_pair, overlap=float(horizon_bars))
        if measured:
            slices[slice_key] = measured
    payload: dict[str, object] = {
        "schema": learning_prior.SCHEMA_VERSION,
        "contract": learning_prior.PRIOR_CONTRACT,
        "generator": "tools/build_learning_prior.py",
        "corpus": {
            "corpus_id": corpus_id,
            "datasets": datasets,
            "pairs": sorted(bars_by_pair),
            "window": {"start": min(stamps).isoformat(), "end": max(stamps).isoformat()},
            "bar_interval_hours": round(source_hours, 6),
            "bar_alignment": BAR_ALIGNMENT,
        },
        "method": {
            "features": list(REPLAYABLE_FEATURES),
            "label": "direction_hit_at_slice_horizon",
            "flat_band": f"atr * {FLAT_ATR_FRACTION}",
            "demean": "pair_direction",
            "effective_n": "raw / horizon_bars",
            "tau_estimator": "dersimonian_laird",
            "tau_used": "upper95",
            "decay_trigger": CONDITION_HIT_RATE_TRIGGER,
            "not_replayable": ["rating_4h", "rating_1d", "tf_agreement", "news_count"],
        },
        "slices": slices,
        "promotion_admissible": False,
    }
    payload["payload_sha256"] = _payload_digest(payload)
    return payload


def _payload_digest(payload: dict[str, object]) -> str:
    """generated_at と自身を除いた正準JSONのハッシュ。決定論の検証に使う。"""
    stripped = {
        key: value
        for key, value in payload.items()
        if key not in ("generated_at", "payload_sha256")
    }
    canonical = json.dumps(stripped, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def render(payload: dict[str, object]) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def summarize(payload: dict[str, object]) -> str:
    lines: list[str] = []
    slices = payload.get("slices")
    assert isinstance(slices, dict)
    for slice_key in sorted(slices):
        features = slices[slice_key]
        assert isinstance(features, dict)
        parts = [
            f"{name} tau<={float(entry['tau_upper95']) * 100:.2f}% (n_eff中央値 "
            f"{entry['n_eff_median']})"
            for name, entry in sorted(features.items())
        ]
        lines.append(f"{slice_key}: " + " / ".join(parts))
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--corpus",
        choices=("csv1h", "m5"),
        default="csv1h",
        help="csv1h=コミット済み1時間足(既定) / m5=M5 bid/askコーパス(15mスライスを含む)",
    )
    parser.add_argument("--bars-root", type=Path, default=DEFAULT_M5_ROOT, help="--corpus m5 用")
    parser.add_argument("--corpus-id", default=None)
    parser.add_argument(
        "--generated-at", default=None, help="ISO時刻を固定する(再現検証用。既定は現在時刻)"
    )
    parser.add_argument("--dry-run", action="store_true", help="書き込まず差分だけ表示する")
    args = parser.parse_args(argv)

    corpus_id = args.corpus_id or (
        DEFAULT_M5_CORPUS_ID if args.corpus == "m5" else DEFAULT_CORPUS_ID
    )
    try:
        payload = build_payload(
            DEFAULT_SOURCES, corpus_id, corpus=args.corpus, m5_root=args.bars_root
        )
    except FileNotFoundError as error:
        print(f"コーパスが見つかりません: {error}", file=sys.stderr)
        return 1
    payload["generated_at"] = args.generated_at or datetime.now(UTC).isoformat()
    print(summarize(payload))

    existing = None
    if args.output.exists():
        try:
            existing = json.loads(args.output.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing = None
    if isinstance(existing, dict):
        before = existing.get("payload_sha256")
        after = payload.get("payload_sha256")
        print(f"payload_sha256: {before} -> {after}" + ("  (変化なし)" if before == after else ""))
    if args.dry_run:
        print("dry-run: 書き込みなし")
        return 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render(payload), encoding="utf-8")
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
