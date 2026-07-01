from __future__ import annotations

from types import SimpleNamespace

import pytest


def _bar(date, o, h, low, c):
    return SimpleNamespace(date=date, open=o, high=h, low=low, close=c)


def test_bars_to_csv_rows_converts_fields():
    import export_history

    bars = [_bar("2024-01-01", 110.0, 111.0, 109.5, 110.5)]
    rows = export_history.bars_to_csv_rows(bars)
    assert rows == [
        {"timestamp": "2024-01-01", "open": 110.0, "high": 111.0, "low": 109.5, "close": 110.5}
    ]


def test_write_csv_round_trips_through_fx_backtester(tmp_path):
    import export_history
    from fx_backtester import data as data_mod

    bars = [
        _bar("2024-01-01", 110.0, 111.0, 109.5, 110.5),
        _bar("2024-01-02", 110.5, 112.0, 110.0, 111.5),
    ]
    out = tmp_path / "history.csv"
    export_history.write_csv(export_history.bars_to_csv_rows(bars), out)

    df = data_mod.load_prices(out)
    assert len(df) == 2
    assert list(df.columns) == ["open", "high", "low", "close"]


def test_write_csv_refuses_empty_rows_and_keeps_existing_file(tmp_path):
    import export_history

    out = tmp_path / "history.csv"
    out.write_text("previous,content\n1,2\n")
    with pytest.raises(ValueError):
        export_history.write_csv([], out)
    assert out.read_text() == "previous,content\n1,2\n"
