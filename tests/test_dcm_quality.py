"""Phase 2: 品質チェックと正規化の検証(ネットワーク非依存)。"""

from __future__ import annotations


import numpy as np
import pandas as pd

from dukascopy_cftc_model.quality import (
    build_report,
    check_cot,
    check_prices,
    normalize_cot,
    normalize_prices,
)


def _clean_prices(n: int = 100, start_price: float = 1.10) -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
    rng = np.random.default_rng(0)
    close = start_price + np.cumsum(rng.normal(0, 0.0005, n))
    return pd.DataFrame(
        {
            "open": close,
            "high": close + 0.0003,
            "low": close - 0.0003,
            "close": close,
            "volume": rng.integers(50, 500, n).astype(float),
        },
        index=pd.DatetimeIndex(idx, name="timestamp"),
    )


def _cot_frame(weeks: int = 30) -> pd.DataFrame:
    dates = pd.date_range("2023-06-06", periods=weeks, freq="7D")
    rng = np.random.default_rng(1)
    nl = rng.integers(100_000, 260_000, weeks)
    ns = rng.integers(100_000, 260_000, weeks)
    return pd.DataFrame(
        {
            "report_date": dates,
            "noncomm_long": nl,
            "noncomm_short": ns,
            "comm_long": rng.integers(300_000, 500_000, weeks),
            "comm_short": rng.integers(300_000, 500_000, weeks),
            "open_interest": rng.integers(600_000, 900_000, weeks),
            "net_noncomm": nl - ns,
        }
    )


def test_check_prices_clean_data_high_coverage() -> None:
    warnings, coverage = check_prices(_clean_prices(), "H1")
    assert coverage >= 0.9
    assert warnings == []


def test_check_prices_flags_nonpositive_and_broken_bars() -> None:
    prices = _clean_prices(50)
    prices.iloc[5, prices.columns.get_loc("low")] = -1.0  # 非正
    prices.iloc[10, prices.columns.get_loc("high")] = prices.iloc[10]["low"] - 0.01  # high<low
    warnings, coverage = check_prices(prices, "H1")
    assert any("非正" in w for w in warnings)
    assert any("high<low" in w for w in warnings)
    assert coverage < 1.0


def test_check_prices_empty() -> None:
    warnings, coverage = check_prices(pd.DataFrame(), "H1")
    assert coverage == 0.0
    assert warnings


def test_check_cot_flags_short_history() -> None:
    warnings, coverage = check_cot(_cot_frame(10), "EUR")
    assert any("少ない" in w for w in warnings)
    assert coverage < 1.0


def test_check_cot_healthy() -> None:
    warnings, coverage = check_cot(_cot_frame(60), "EUR")
    assert coverage >= 0.9
    assert warnings == []


def test_check_cot_empty() -> None:
    warnings, coverage = check_cot(pd.DataFrame(columns=["report_date"]), "JPY")
    assert coverage == 0.0
    assert warnings


def test_build_report_combines_price_and_cot() -> None:
    report = build_report(
        _clean_prices(),
        {"EUR": _cot_frame(60), "USD": _cot_frame(60)},
        "H1",
    )
    assert report.price_bars == 100
    assert report.is_usable
    assert 0.0 <= report.coverage <= 1.0
    assert "coverage" in report.summary()


def test_normalize_prices_adds_log_return_and_drops_bad_bars() -> None:
    prices = _clean_prices(20)
    prices.iloc[3, prices.columns.get_loc("close")] = -5.0  # 非正 → 除去対象
    out = normalize_prices(prices)
    assert "log_return" in out.columns
    assert (out[["open", "high", "low", "close"]] > 0).all().all()
    assert out.index.is_monotonic_increasing


def test_normalize_prices_deduplicates_index() -> None:
    prices = _clean_prices(5)
    dup = pd.concat([prices, prices.iloc[[0]]])
    out = normalize_prices(dup)
    assert not out.index.has_duplicates


def test_normalize_cot_sorts_and_dedups() -> None:
    frame = _cot_frame(10)
    shuffled = pd.concat([frame.iloc[::-1], frame.iloc[[0]]])  # 逆順+重複
    out = normalize_cot(shuffled)
    assert out["report_date"].is_monotonic_increasing
    assert not out["report_date"].duplicated().any()
