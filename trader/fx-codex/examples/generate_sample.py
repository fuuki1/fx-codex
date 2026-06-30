"""決定論的なサンプル価格/イベントを生成する（seeded）。

再現性のため固定シード。MA クロスがトレードを生む程度のトレンド/サイクル/ノイズを持つ
日次 USDJPY 風 OHLC を作る。`python generate_sample.py` で再生成可能。
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).parent


def main() -> None:
    rng = np.random.default_rng(42)
    n = 900
    dates = pd.bdate_range("2021-01-01", periods=n, tz="UTC")
    t = np.arange(n)

    drift = 0.02 * t
    cycle = 6.0 * np.sin(2 * np.pi * t / 120.0)   # 緩やかなサイクルでクロスを発生
    noise = np.cumsum(rng.normal(0.0, 0.3, n))
    close = np.maximum(110.0 + drift + cycle + noise, 50.0)

    open_ = np.concatenate([[close[0]], close[:-1]])
    rng_pct = rng.uniform(0.001, 0.004, n)
    high = np.maximum(close * (1 + rng_pct), np.maximum(open_, close))
    low = np.minimum(close * (1 - rng_pct), np.minimum(open_, close))

    prices = pd.DataFrame(
        {"timestamp": dates, "open": open_, "high": high, "low": low, "close": close}
    )
    prices.to_csv(HERE / "sample_prices.csv", index=False, float_format="%.5f")

    ev_idx = [100, 250, 400, 550, 700]
    events = pd.DataFrame(
        {"timestamp": [dates[i] for i in ev_idx], "kind": ["high_impact"] * len(ev_idx)}
    )
    events.to_csv(HERE / "sample_events.csv", index=False)
    print(f"wrote {n} bars -> sample_prices.csv, {len(ev_idx)} events -> sample_events.csv")


if __name__ == "__main__":
    main()
