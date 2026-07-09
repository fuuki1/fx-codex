"""Phase 1: Dukascopy .bi5 デコードと CFTC COT時系列パースの検証。

すべてネットワーク非依存。自作のバイト列/JSON配列フィクスチャで完結する。
"""

from __future__ import annotations

import lzma
from datetime import date, datetime, UTC


from dukascopy_cftc_model.cftc import _cache_covers_start, parse_cot_history
from dukascopy_cftc_model.dukascopy import (
    TICK_STRUCT,
    decode_bi5,
    decompress_bi5,
    point_value,
    ticks_to_ohlcv,
)


def _make_bi5_bytes(ticks: list[tuple[int, int, int, float, float]]) -> bytes:
    """(ms, ask_int, bid_int, ask_vol, bid_vol) のリストを .bi5 生バイト列へ。"""
    return b"".join(TICK_STRUCT.pack(*t) for t in ticks)


def test_point_value_jpy_vs_non_jpy() -> None:
    assert point_value("EURUSD") == 100_000  # pip_size=0.0001 → 1e5
    assert point_value("USDJPY") == 1_000  # pip_size=0.01 → 1e3


def test_decode_bi5_recovers_prices_and_times() -> None:
    hour = datetime(2024, 1, 2, 0, 0, tzinfo=UTC)
    raw = _make_bi5_bytes(
        [
            (1340, 110370, 110366, 3.6, 0.9),
            (1546, 110371, 110366, 6.3, 0.9),
        ]
    )
    ticks = decode_bi5(raw, hour, "EURUSD")
    assert len(ticks) == 2
    assert abs(ticks[0].ask - 1.10370) < 1e-9
    assert abs(ticks[0].bid - 1.10366) < 1e-9
    assert abs(ticks[0].mid - (1.10370 + 1.10366) / 2) < 1e-9
    from datetime import timedelta

    assert ticks[0].when == hour + timedelta(milliseconds=1340)  # 1.340秒後
    assert ticks[1].when.second == 1  # 1546ms → 1秒546ms


def test_decode_bi5_ignores_trailing_garbage() -> None:
    hour = datetime(2024, 1, 2, 0, 0, tzinfo=UTC)
    raw = _make_bi5_bytes([(0, 110000, 109990, 1.0, 1.0)]) + b"\x00\x03"  # 端数バイト
    ticks = decode_bi5(raw, hour, "EURUSD")
    assert len(ticks) == 1


def test_decompress_bi5_roundtrip_and_empty() -> None:
    raw = _make_bi5_bytes([(0, 110000, 109990, 1.0, 1.0)])
    compressed = lzma.compress(raw)
    assert decompress_bi5(compressed) == raw
    assert decompress_bi5(b"") == b""


def test_ticks_to_ohlcv_h1_aggregation() -> None:
    hour = datetime(2024, 1, 2, 0, 0, tzinfo=UTC)
    # 3 ticks in the same hour: mid = 1.1, 1.2, 1.05 → O=1.1 H=1.2 L=1.05 C=1.05
    raw = _make_bi5_bytes(
        [
            (0, 110000, 110000, 1.0, 1.0),  # mid 1.10
            (1000, 120000, 120000, 1.0, 1.0),  # mid 1.20
            (2000, 105000, 105000, 1.0, 1.0),  # mid 1.05
        ]
    )
    ticks = decode_bi5(raw, hour, "EURUSD")
    ohlcv = ticks_to_ohlcv(ticks, "H1")
    assert len(ohlcv) == 1
    row = ohlcv.iloc[0]
    assert abs(row["open"] - 1.10) < 1e-9
    assert abs(row["high"] - 1.20) < 1e-9
    assert abs(row["low"] - 1.05) < 1e-9
    assert abs(row["close"] - 1.05) < 1e-9
    assert row["volume"] == 3


def test_ticks_to_ohlcv_empty_returns_empty_frame() -> None:
    frame = ticks_to_ohlcv([], "H1")
    assert frame.empty
    assert list(frame.columns) == ["open", "high", "low", "close", "volume"]


# ----------------------------------------------------------------- CFTC COT


def _cot_row(d: str, nl: int, ns: int, cl: int, cs: int, oi: int) -> dict[str, str]:
    return {
        "report_date_as_yyyy_mm_dd": f"{d}T00:00:00.000",
        "noncomm_positions_long_all": str(nl),
        "noncomm_positions_short_all": str(ns),
        "comm_positions_long_all": str(cl),
        "comm_positions_short_all": str(cs),
        "open_interest_all": str(oi),
    }


def test_parse_cot_history_builds_sorted_timeseries() -> None:
    payload = [
        _cot_row("2024-01-16", 200, 100, 300, 350, 700),
        _cot_row("2024-01-02", 150, 120, 280, 330, 650),  # 古い方を後に置く
        _cot_row("2024-01-09", 180, 110, 290, 340, 680),
    ]
    frame = parse_cot_history(payload)
    assert len(frame) == 3
    # 昇順ソートされている
    assert list(frame["report_date"].dt.date) == [
        date(2024, 1, 2),
        date(2024, 1, 9),
        date(2024, 1, 16),
    ]
    # net = long - short
    assert frame.iloc[0]["net_noncomm"] == 30  # 150-120
    assert frame.iloc[2]["net_noncomm"] == 100  # 200-100


def test_parse_cot_history_skips_unparseable_rows() -> None:
    payload = [
        _cot_row("2024-01-02", 150, 120, 280, 330, 650),
        {"report_date_as_yyyy_mm_dd": "bad-date", "noncomm_positions_long_all": "1"},
        {"report_date_as_yyyy_mm_dd": "2024-01-09T00:00:00.000"},  # 投機筋欠損
    ]
    frame = parse_cot_history(payload)
    assert len(frame) == 1


def test_parse_cot_history_empty_input() -> None:
    frame = parse_cot_history([])
    assert frame.empty
    assert "net_noncomm" in frame.columns
    frame2 = parse_cot_history("not a list")
    assert frame2.empty


def test_cache_covers_start_detects_insufficient_history() -> None:
    # キャッシュが 2024-01 以降しか無いのに 2022 を要求 → カバー不足(False)。
    payload = {
        "rows": [
            _cot_row("2024-01-09", 200, 100, 300, 350, 700),
            _cot_row("2024-01-16", 210, 110, 310, 360, 710),
        ]
    }
    assert _cache_covers_start(payload, date(2022, 1, 1)) is False
    # 要求 start がキャッシュ最古行の範囲内 → カバーOK(True)。
    assert _cache_covers_start(payload, date(2024, 6, 1)) is True
    # 空/壊れは False
    assert _cache_covers_start({"rows": []}, date(2022, 1, 1)) is False
