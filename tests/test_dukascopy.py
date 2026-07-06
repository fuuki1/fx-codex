"""Dukascopy 実ティック層(dukascopy.py)のテスト(ネットワーク不要)。

.bi5 は「LZMA圧縮された >IIIff 固定長レコード列」なので、テストでは同じ形式の
バイト列を lzma+struct で合成し、パース→バー集約→CSV→将来価格供給を丸ごと検証する。
ネットワークに触れる fetch_* はモックセッション(requests.get 互換)で確認する。
"""

from __future__ import annotations

import lzma
import struct
from datetime import datetime, timedelta, UTC

import pytest

from fx_intel import dukascopy as dk

_STRUCT = struct.Struct(">IIIff")


def _make_bi5(records: list[tuple[int, int, int, float, float]]) -> bytes:
    """(ms_offset, ask_pts, bid_pts, ask_vol, bid_vol) 列を .bi5 バイト列に固める。"""
    payload = b"".join(_STRUCT.pack(*rec) for rec in records)
    return lzma.compress(payload)


BASE = datetime(2025, 6, 2, 9, 0, tzinfo=UTC)  # 月曜 09:00 UTC


# ---------------------------------------------------------------- point_value


def test_point_value_jpy_is_three_digit() -> None:
    assert dk.point_value("USDJPY") == dk.POINT_VALUE_3DIGIT
    assert dk.point_value("EURJPY") == dk.POINT_VALUE_3DIGIT


def test_point_value_non_jpy_is_five_digit() -> None:
    assert dk.point_value("EURUSD") == dk.POINT_VALUE_5DIGIT
    assert dk.point_value("gbpusd") == dk.POINT_VALUE_5DIGIT


# ---------------------------------------------------------------- parse_bi5


def test_parse_bi5_reconstructs_prices_and_absolute_time() -> None:
    # USDJPY: point=1e-3。ask=143500pt→143.500、bid=143480pt→143.480
    raw = _make_bi5([(0, 143500, 143480, 1.5, 2.0), (60_000, 143520, 143500, 1.0, 1.0)])
    ticks = dk.parse_bi5(raw, BASE, "USDJPY")
    assert len(ticks) == 2
    assert ticks[0].when == BASE
    assert ticks[0].ask == pytest.approx(143.500)
    assert ticks[0].bid == pytest.approx(143.480)
    assert ticks[0].mid == pytest.approx(143.490)
    assert ticks[0].spread == pytest.approx(0.020)
    # 2件目は +60000ms = base + 1分
    assert ticks[1].when == BASE + timedelta(minutes=1)


def test_parse_bi5_empty_is_no_ticks() -> None:
    assert dk.parse_bi5(b"", BASE, "USDJPY") == []


def test_parse_bi5_corrupt_lzma_returns_empty() -> None:
    assert dk.parse_bi5(b"not-lzma-data", BASE, "USDJPY") == []


def test_parse_bi5_ignores_trailing_partial_record() -> None:
    payload = _STRUCT.pack(0, 143500, 143480, 1.0, 1.0) + b"\x00\x00\x00"  # 端数3バイト
    raw = lzma.compress(payload)
    ticks = dk.parse_bi5(raw, BASE, "USDJPY")
    assert len(ticks) == 1  # 端数は無視


# ---------------------------------------------------------------- ticks_to_bars


def _tick(offset_min: float, bid: float, ask: float) -> dk.Tick:
    return dk.Tick(
        when=BASE + timedelta(minutes=offset_min),
        bid=bid,
        ask=ask,
        bid_volume=1.0,
        ask_volume=1.0,
    )


def test_ticks_to_bars_aggregates_ohlc_on_mid() -> None:
    # 5分足に、9:00〜9:04 の3ティック(同一足)を入れる
    ticks = [
        _tick(0.0, 143.480, 143.500),  # mid 143.490 = open
        _tick(2.0, 143.520, 143.540),  # mid 143.530 = high
        _tick(4.0, 143.460, 143.480),  # mid 143.470 = low, close
    ]
    bars = dk.ticks_to_bars(ticks, "5m")
    assert len(bars) == 1
    bar = bars[0]
    assert bar.timestamp == BASE  # 09:00 の足
    assert bar.open == pytest.approx(143.490)
    assert bar.high == pytest.approx(143.530)
    assert bar.low == pytest.approx(143.470)
    assert bar.close == pytest.approx(143.470)
    assert bar.volume == 3
    assert bar.spread == pytest.approx(0.020)  # 全ティックspread=0.020の平均


def test_ticks_to_bars_splits_across_boundaries_and_skips_empty() -> None:
    ticks = [_tick(0.0, 1.0, 1.0002), _tick(7.0, 1.001, 1.0012)]  # 09:00足 と 09:05足
    bars = dk.ticks_to_bars(ticks, "5m")
    assert [b.timestamp for b in bars] == [BASE, BASE + timedelta(minutes=5)]
    # ティックの無い 09:10 以降のバーは捏造しない
    assert len(bars) == 2


def test_ticks_to_bars_daily_uses_utc_day_boundary() -> None:
    ticks = [_tick(0.0, 1.0, 1.0), _tick(60 * 5, 2.0, 2.0)]  # 同じUTC日内
    bars = dk.ticks_to_bars(ticks, "1d")
    assert len(bars) == 1
    assert bars[0].timestamp == BASE.replace(hour=0, minute=0)


def test_ticks_to_bars_rejects_unknown_timeframe() -> None:
    with pytest.raises(ValueError):
        dk.ticks_to_bars([_tick(0.0, 1.0, 1.0)], "2h")


def test_ticks_to_bars_empty_input() -> None:
    assert dk.ticks_to_bars([], "1h") == []


# ---------------------------------------------------------------- bars_to_csv_rows


def test_bars_to_csv_rows_matches_backtester_schema() -> None:
    bars = [dk.Bar(BASE, 143.49, 143.53, 143.47, 143.47, 3, 0.02)]
    rows = dk.bars_to_csv_rows(bars, "USDJPY")
    assert rows[0] == "timestamp,symbol,open,high,low,close,volume,spread_price"
    # JPYは3桁丸め
    assert rows[1] == "2025-06-02 09:00:00,USDJPY,143.490,143.530,143.470,143.470,3,0.020"


def test_bars_to_csv_rows_non_jpy_five_digits() -> None:
    bars = [dk.Bar(BASE, 1.08123, 1.08160, 1.08100, 1.08150, 5, 0.00008)]
    rows = dk.bars_to_csv_rows(bars, "EURUSD")
    assert rows[1].startswith(
        "2025-06-02 09:00:00,EURUSD,1.08123,1.08160,1.08100,1.08150,5,0.00008"
    )


# ---------------------------------------------------------------- fetch(モックセッション)


class _FakeResponse:
    def __init__(self, content: bytes, status_code: int = 200) -> None:
        self.content = content
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeSession:
    """slot時刻 -> bytes(または404)を返す最小の requests.Session 互換。"""

    def __init__(self, by_hour: dict[int, bytes | None]) -> None:
        self.by_hour = by_hour
        self.calls: list[str] = []

    def get(self, url: str, headers=None, timeout=None) -> _FakeResponse:
        self.calls.append(url)
        # URLの "..../HHh_ticks.bi5" から時刻を拾う
        hour = int(url.rsplit("/", 1)[1][:2])
        body = self.by_hour.get(hour)
        if body is None:
            return _FakeResponse(b"", status_code=404)
        return _FakeResponse(body)


def test_fetch_ticks_uses_session_and_caches(tmp_path) -> None:
    raw = _make_bi5([(0, 143500, 143480, 1.0, 1.0)])
    session = _FakeSession({9: raw, 10: None})  # 10時は404(休場)
    ticks = dk.fetch_ticks("USDJPY", BASE, BASE + timedelta(hours=1), tmp_path, session=session)
    assert len(ticks) == 1
    assert ticks[0].bid == pytest.approx(143.480)
    # 9時・10時の2スロットを取得しに行っている
    assert len(session.calls) == 2

    # 2回目はキャッシュから読むのでネットワークに触れない
    session2 = _FakeSession({})
    ticks2 = dk.fetch_ticks("USDJPY", BASE, BASE + timedelta(hours=1), tmp_path, session=session2)
    assert len(ticks2) == 1
    assert session2.calls == []  # キャッシュヒットで get 未呼び出し


def test_fetch_ticks_404_is_not_an_error(tmp_path) -> None:
    session = _FakeSession({9: None})  # 完全休場
    warnings: list[str] = []
    ticks = dk.fetch_ticks("USDJPY", BASE, BASE, tmp_path, warnings=warnings, session=session)
    assert ticks == []
    assert warnings == []  # 404は警告を出さない


def test_fetch_ticks_rejects_oversized_range(tmp_path) -> None:
    with pytest.raises(ValueError):
        dk.fetch_ticks(
            "USDJPY",
            BASE,
            BASE + timedelta(days=200),
            tmp_path,
            max_hours=24,
        )


def test_fetch_ticks_url_encodes_zero_based_month(tmp_path) -> None:
    session = _FakeSession({9: None})
    dk.fetch_ticks("USDJPY", BASE, BASE, tmp_path, session=session)
    # 2025年6月 → year=2025, month0=05, day=02, hour=09
    assert session.calls[0].endswith("USDJPY/2025/05/02/09h_ticks.bi5")


# ---------------------------------------------------------------- download_bars_csv


def test_download_bars_csv_writes_readable_file(tmp_path) -> None:
    raw = _make_bi5([(0, 143500, 143480, 1.0, 1.0), (1_800_000, 143600, 143580, 1.0, 1.0)])  # +30分
    session = _FakeSession({9: raw})
    out = tmp_path / "out" / "USDJPY_1h.csv"
    result = dk.download_bars_csv(
        "USDJPY", BASE, BASE, "1h", out, tmp_path / "cache", session=session
    )
    assert result.out_path == out
    assert result.bar_count == 1
    assert result.tick_count == 2
    text = out.read_text(encoding="utf-8")
    assert text.splitlines()[0] == "timestamp,symbol,open,high,low,close,volume,spread_price"
    assert "2025-06-02 09:00:00,USDJPY" in text


def test_download_bars_csv_no_ticks_writes_nothing(tmp_path) -> None:
    session = _FakeSession({9: None})
    out = tmp_path / "empty.csv"
    result = dk.download_bars_csv(
        "USDJPY", BASE, BASE, "1h", out, tmp_path / "cache", session=session
    )
    assert result.out_path is None
    assert result.bar_count == 0
    assert not out.exists()
    assert result.warnings  # 理由が残る


def test_download_bars_csv_output_loads_in_backtester(tmp_path) -> None:
    """出力CSVが fx_backtester のローダをそのまま通ることを保証(契約テスト)。"""
    pytest.importorskip("pandas")
    from fx_backtester.data import load_price_csv

    # 1時間ぶん、5分おきにティックを置いて複数バーを作る
    records = [(m * 300_000, 143500 + m * 10, 143480 + m * 10, 1.0, 1.0) for m in range(12)]
    session = _FakeSession({9: _make_bi5(records)})
    out = tmp_path / "USDJPY_5m.csv"
    dk.download_bars_csv("USDJPY", BASE, BASE, "5m", out, tmp_path / "cache", session=session)

    loaded = load_price_csv(out)
    assert "USDJPY" in loaded
    frame = loaded["USDJPY"]
    assert not frame.empty
    assert (frame["high"] >= frame["low"]).all()  # OHLC整合
    assert (frame["spread_price"] > 0).all()  # spread>0(品質ゲートを通る)


# ---------------------------------------------------------------- 源B: FuturePriceProvider


def test_future_price_provider_returns_nearest_mid(tmp_path) -> None:
    # target=09:30 の近傍に、09:00〜09:59 のティックを流す
    records = [(m * 60_000, 143500 + m, 143480 + m, 1.0, 1.0) for m in range(60)]
    session = _FakeSession({9: _make_bi5(records), 8: None, 10: None})
    provider = dk.make_future_price_provider(tmp_path, session=session)
    target = BASE + timedelta(minutes=30)  # 09:30
    price = provider("USDJPY", "1h", target, 1.0)
    assert price is not None
    # 09:30 のティックは m=30: ask=143530pt→143.530, bid=143510pt→143.510, mid=143.520
    assert price == pytest.approx(143.520, abs=1e-3)


def test_future_price_provider_returns_none_when_no_data(tmp_path) -> None:
    session = _FakeSession({})  # 全404
    provider = dk.make_future_price_provider(tmp_path, session=session)
    assert provider("USDJPY", "1h", BASE, 1.0) is None


def test_future_price_provider_swallows_errors(tmp_path) -> None:
    """源Bは失敗しても採点を止めず静かに None を返す(判断ログを汚さない)。"""

    class _Boom:
        def get(self, *a, **k):
            raise RuntimeError("network down")

    provider = dk.make_future_price_provider(tmp_path, session=_Boom())
    # RuntimeError は provider 内で握りつぶされ None になる
    assert provider("USDJPY", "1h", BASE, 1.0) is None


def test_nearest_mid_empty() -> None:
    assert dk.nearest_mid([], BASE) is None


def test_nearest_mid_picks_closest() -> None:
    ticks = [_tick(0.0, 1.0, 1.0002), _tick(10.0, 2.0, 2.0002), _tick(20.0, 3.0, 3.0002)]
    # target=09:12 → 09:10 のティック(mid≈2.0001)が最も近い
    got = dk.nearest_mid(ticks, BASE + timedelta(minutes=12))
    assert got == pytest.approx(2.0001, abs=1e-3)
