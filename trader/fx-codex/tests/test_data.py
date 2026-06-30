from __future__ import annotations

import pandas as pd
import pytest
from fx_backtester import data as d


def test_load_prices_valid(sample_df):
    assert list(sample_df.columns) == ["open", "high", "low", "close"]
    assert isinstance(sample_df.index, pd.DatetimeIndex)
    assert sample_df.index.is_monotonic_increasing
    assert not sample_df.index.has_duplicates
    assert len(sample_df) == 900


def test_load_prices_rejects_bad_ohlc(tmp_path):
    p = tmp_path / "bad.csv"
    p.write_text("timestamp,open,high,low,close\n2021-01-01,100,99,101,100\n")  # high<low
    with pytest.raises(d.DataError):
        d.load_prices(p)


def test_load_prices_rejects_duplicate_ts(tmp_path):
    p = tmp_path / "dup.csv"
    p.write_text(
        "timestamp,open,high,low,close\n"
        "2021-01-01,100,101,99,100\n"
        "2021-01-01,100,101,99,100\n"
    )
    with pytest.raises(d.DataError):
        d.load_prices(p)


def test_infer_periods_per_year_daily(sample_df):
    assert d.infer_periods_per_year(sample_df.index) == 252.0


def test_blocked_mask_aligns_to_events(sample_df, sample_events_path):
    events = d.load_events(sample_events_path)
    mask = d.blocked_mask(sample_df.index, events)
    assert mask.sum() == 5  # 5 つの high_impact イベントが index 上に存在
