from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd


def main() -> None:
    output_dir = Path(__file__).resolve().parent
    rng = np.random.default_rng(42)
    index = pd.date_range("2024-01-01 00:00:00", periods=900, freq="h")
    specs = {
        "USDJPY": {"start": 145.0, "vol": 0.08},
        "EURUSD": {"start": 1.09, "vol": 0.0007},
        "GBPUSD": {"start": 1.27, "vol": 0.0009},
    }

    rows = []
    for symbol, spec in specs.items():
        close = spec["start"] + np.cumsum(rng.normal(0, spec["vol"], len(index)))
        open_ = np.r_[close[0], close[:-1]]
        bar_range = np.abs(rng.normal(spec["vol"] * 1.5, spec["vol"] * 0.4, len(index)))
        high = np.maximum(open_, close) + bar_range
        low = np.minimum(open_, close) - bar_range
        for timestamp, open_price, high_price, low_price, close_price in zip(index, open_, high, low, close):
            rows.append(
                {
                    "timestamp": timestamp,
                    "symbol": symbol,
                    "open": round(float(open_price), 6),
                    "high": round(float(high_price), 6),
                    "low": round(float(low_price), 6),
                    "close": round(float(close_price), 6),
                }
            )

    pd.DataFrame(rows).to_csv(output_dir / "sample_prices.csv", index=False)
    pd.DataFrame(
        [
            {
                "timestamp": "2024-01-05 22:30:00",
                "currency": "USD",
                "symbol": "",
                "impact": "high",
                "name": "Nonfarm payrolls sample",
            },
            {
                "timestamp": "2024-01-17 16:00:00",
                "currency": "GBP",
                "symbol": "GBPUSD",
                "impact": "medium",
                "name": "CPI sample",
            },
        ]
    ).to_csv(output_dir / "sample_events.csv", index=False)
    print(f"Wrote {output_dir / 'sample_prices.csv'}")
    print(f"Wrote {output_dir / 'sample_events.csv'}")


if __name__ == "__main__":
    main()
