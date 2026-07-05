from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from fx_intel.historical_backfill import HistoricalBackfillConfig, run_backfill
from fx_intel.tf_learning import load_timeframe_learning


def _write_prices(path: Path, symbol: str = "USDJPY", periods: int = 220) -> None:
    rows = []
    timestamps = pd.date_range("2025-01-06 00:00:00", periods=periods, freq="15min")
    price = 150.0
    for index, timestamp in enumerate(timestamps):
        drift = index * 0.015
        close = price + drift
        rows.append(
            {
                "timestamp": timestamp.strftime("%Y-%m-%d %H:%M:%S"),
                "symbol": symbol,
                "open": close - 0.01,
                "high": close + 0.03,
                "low": close - 0.03,
                "close": close,
            }
        )
    pd.DataFrame(rows).to_csv(path, index=False)


def test_historical_backfill_writes_learning_artifacts(tmp_path: Path) -> None:
    csv_path = tmp_path / "USDJPY_15m.csv"
    out_dir = tmp_path / "ai_backfill"
    _write_prices(csv_path)

    result = run_backfill(
        HistoricalBackfillConfig(
            data_paths=(csv_path,),
            output_dir=out_dir,
            timeframes=("15m", "1h"),
            fast_window=3,
            slow_window=6,
            atr_window=3,
            adx_window=3,
        )
    )

    assert result.price_rows > 0
    assert result.journal_rows > 0
    assert result.artifacts["prices"].exists()
    assert result.artifacts["journal"].exists()
    assert result.artifacts["learning"].exists()
    assert result.artifacts["quality"].exists()
    assert result.artifacts["manifest"].exists()

    manifest = json.loads(result.artifacts["manifest"].read_text(encoding="utf-8"))
    assert manifest["symbols"] == ["USDJPY"]
    assert manifest["journal_rows"] == result.journal_rows

    journal_first = json.loads(
        result.artifacts["journal"].read_text(encoding="utf-8").splitlines()[0]
    )
    assert journal_first["source"] == "historical_chart_backfill"
    assert journal_first["timeframe"] in {"15m", "1h"}
    assert journal_first["horizon_hours"] in {0.25, 1.0}

    quality = pd.read_csv(result.artifacts["quality"])
    assert set(quality["timeframe"]) == {"15m", "1h"}
    assert quality["evaluated"].sum() > 0


def test_currency_score_csv_feeds_historical_news_score(tmp_path: Path) -> None:
    csv_path = tmp_path / "USDJPY_15m.csv"
    score_path = tmp_path / "scores.csv"
    out_dir = tmp_path / "ai_backfill"
    _write_prices(csv_path)
    pd.DataFrame(
        [
            {
                "timestamp": "2025-01-06 00:00:00",
                "currency": "USD",
                "score": 0.8,
                "headline_count": 5,
            },
            {
                "timestamp": "2025-01-06 00:00:00",
                "currency": "JPY",
                "score": -0.4,
                "headline_count": 5,
            },
        ]
    ).to_csv(score_path, index=False)

    result = run_backfill(
        HistoricalBackfillConfig(
            data_paths=(csv_path,),
            output_dir=out_dir,
            timeframes=("15m",),
            fast_window=3,
            slow_window=6,
            atr_window=3,
            adx_window=3,
            currency_score_csv=score_path,
        )
    )

    rows = [
        json.loads(line)
        for line in result.artifacts["journal"].read_text(encoding="utf-8").splitlines()
    ]
    assert any(row["news_score"] > 0 for row in rows)
    assert any(
        component["key"] == "news" and component["weight"] > 0
        for row in rows
        for component in row["components"]
    )


def test_historical_backfill_installs_quality_gated_baseline(tmp_path: Path) -> None:
    csv_path = tmp_path / "USDJPY_15m.csv"
    out_dir = tmp_path / "ai_backfill"
    baseline_path = tmp_path / "logs" / "briefing_tf_baseline.json"
    _write_prices(csv_path, periods=260)

    result = run_backfill(
        HistoricalBackfillConfig(
            data_paths=(csv_path,),
            output_dir=out_dir,
            timeframes=("15m",),
            fast_window=3,
            slow_window=6,
            atr_window=3,
            adx_window=3,
            install_baseline_path=baseline_path,
            baseline_min_evaluated=1,
        )
    )

    assert result.artifacts["baseline"] == baseline_path
    payload = json.loads(baseline_path.read_text(encoding="utf-8"))
    assert payload["baseline"]["source"] == "historical_chart_backfill"
    loaded = load_timeframe_learning(baseline_path)
    assert ("USDJPY", "15m") in loaded.profiles


def test_baseline_install_requires_quality_gate(tmp_path: Path) -> None:
    csv_path = tmp_path / "USDJPY_15m.csv"
    out_dir = tmp_path / "ai_backfill"
    baseline_path = tmp_path / "logs" / "briefing_tf_baseline.json"
    _write_prices(csv_path, periods=30)

    with pytest.raises(ValueError, match="baseline quality gate failed"):
        run_backfill(
            HistoricalBackfillConfig(
                data_paths=(csv_path,),
                output_dir=out_dir,
                timeframes=("1h",),
                fast_window=3,
                slow_window=6,
                atr_window=3,
                adx_window=3,
                install_baseline_path=baseline_path,
                baseline_min_evaluated=100,
            )
        )
