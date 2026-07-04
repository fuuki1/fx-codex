"""時間足別ジャーナル + 将来価格調達(price_history)のテスト(ネットワーク不要)。"""

from __future__ import annotations

from datetime import datetime, timedelta, UTC

from fx_intel import price_history as ph
from fx_intel.journal import append_timeframe_plans, read_entries
from fx_intel.timeframe import TimeframePlan

# 月曜 09:00 UTC = 市場オープン中
T0 = datetime(2026, 6, 29, 9, 0, tzinfo=UTC)


def _plan(timeframe: str, horizon: float, direction: str, close: float) -> TimeframePlan:
    return TimeframePlan(
        symbol="USDJPY",
        timeframe=timeframe,
        horizon_hours=horizon,
        direction=direction,
        conviction=50,
        tf_score=0.5,
        news_score=0.1,
        composite=0.3,
        close=close,
        atr=0.1,
        rsi=55.0,
        adx=25.0,
    )


# ------------------------------------------------- journal 拡張


def test_append_timeframe_plans_writes_timeframe_and_horizon(tmp_path) -> None:
    path = tmp_path / "journal.jsonl"
    append_timeframe_plans(path, [_plan("15m", 0.25, "long", 150.0)], now=T0)
    entries = list(read_entries(path))
    assert len(entries) == 1
    row = entries[0]
    assert row["timeframe"] == "15m"
    assert row["horizon_hours"] == 0.25
    assert row["tech_score"] == 0.5  # tf_score を tech_score キーで記録(learning 互換)
    assert row["rsi"] == 55.0
    assert row["close"] == 150.0


def test_append_timeframe_plans_appends(tmp_path) -> None:
    path = tmp_path / "journal.jsonl"
    append_timeframe_plans(path, [_plan("1h", 1.0, "long", 156.0)], now=T0)
    append_timeframe_plans(path, [_plan("1h", 1.0, "long", 156.3)], now=T0 + timedelta(hours=1))
    assert len(list(read_entries(path))) == 2


# ------------------------------------------------- build_close_series


def test_build_close_series_splits_by_symbol_and_timeframe() -> None:
    entries = [
        {"ts": T0.isoformat(), "symbol": "USDJPY", "timeframe": "1h", "close": 156.0},
        {"ts": T0.isoformat(), "symbol": "USDJPY", "timeframe": "15m", "close": 150.0},
        {"ts": T0.isoformat(), "symbol": "EURUSD", "timeframe": "1h", "close": 1.08},
    ]
    series = ph.build_close_series(entries)
    assert set(series.keys()) == {("USDJPY", "1h"), ("USDJPY", "15m"), ("EURUSD", "1h")}


def test_build_close_series_legacy_rows_use_empty_timeframe() -> None:
    # timeframe を持たない旧スキーマ行(融合1判断)は "" キーに入る
    entries = [{"ts": T0.isoformat(), "symbol": "USDJPY", "close": 156.0}]
    series = ph.build_close_series(entries)
    assert ("USDJPY", "") in series


def test_build_close_series_skips_non_numeric_and_bool_close() -> None:
    entries: list[dict] = [
        {"ts": T0.isoformat(), "symbol": "USDJPY", "timeframe": "1h", "close": None},
        {"ts": T0.isoformat(), "symbol": "USDJPY", "timeframe": "1h", "close": True},
        {"ts": "bad-ts", "symbol": "USDJPY", "timeframe": "1h", "close": 156.0},
    ]
    assert ph.build_close_series(entries) == {}


def test_series_is_time_sorted() -> None:
    entries = [
        {
            "ts": (T0 + timedelta(hours=2)).isoformat(),
            "symbol": "U",
            "timeframe": "1h",
            "close": 3.0,
        },
        {"ts": T0.isoformat(), "symbol": "U", "timeframe": "1h", "close": 1.0},
        {
            "ts": (T0 + timedelta(hours=1)).isoformat(),
            "symbol": "U",
            "timeframe": "1h",
            "close": 2.0,
        },
    ]
    series = ph.build_close_series(entries)[("U", "1h")]
    assert [c for _, c in series] == [1.0, 2.0, 3.0]


# ------------------------------------------------- future_close / resolve


def _series(points: list[tuple[datetime, float]]) -> list[tuple[datetime, float]]:
    return sorted(points, key=lambda p: p[0])


def test_future_close_picks_point_nearest_horizon() -> None:
    # 主ホライズン60分に対し、58分(=2分ずれ)を50分(=10分ずれ)より優先する
    series = _series(
        [
            (T0 + timedelta(minutes=50), 156.1),  # 50分 = ホライズンから10分
            (T0 + timedelta(minutes=58), 156.4),  # 58分 = ホライズンから2分(最近傍)
        ]
    )
    close = ph.future_close_from_series(series, T0, horizon_hours=1.0, tolerance_hours=0.25)
    assert close == 156.4


def test_future_close_returns_none_when_no_point_in_window() -> None:
    series = _series([(T0 + timedelta(hours=5), 156.0)])  # 主ホライズン1hの窓外
    assert ph.future_close_from_series(series, T0, 1.0, 0.25) is None


def test_future_close_empty_series() -> None:
    assert ph.future_close_from_series([], T0, 1.0, 0.25) is None


def test_resolve_prefers_journal_then_provider() -> None:
    series_by_key = {("USDJPY", "1h"): _series([(T0 + timedelta(hours=1), 156.3)])}
    # 源Aで見つかる → provider は呼ばれない
    got = ph.resolve_future_close(
        series_by_key, "USDJPY", "1h", T0, 1.0, 0.25, provider=lambda *a: 999.0
    )
    assert got == 156.3
    # 源Aに無い(後続なし) → provider にフォールバック
    got_b = ph.resolve_future_close(
        series_by_key,
        "USDJPY",
        "1h",
        T0 + timedelta(hours=1),
        1.0,
        0.25,
        provider=lambda *a: 999.0,
    )
    assert got_b == 999.0


def test_resolve_returns_none_without_provider() -> None:
    got = ph.resolve_future_close({}, "USDJPY", "1h", T0, 1.0, 0.25)
    assert got is None


def test_resolve_passes_target_time_to_provider() -> None:
    captured: dict[str, object] = {}

    def provider(symbol, timeframe, target_time, tol):
        captured["target_time"] = target_time
        captured["timeframe"] = timeframe
        return 1.0

    ph.resolve_future_close({}, "USDJPY", "4h", T0, 4.0, 1.0, provider=provider)
    assert captured["timeframe"] == "4h"
    assert captured["target_time"] == T0 + timedelta(hours=4)


# ------------------------------------------------- snapshot_entries


def test_snapshot_entries_converts_current_closes() -> None:
    rows = ph.snapshot_entries({"USDJPY": {"1h": 157.0, "4h": 157.2}}, now=T0)
    assert len(rows) == 2
    assert all(r["ts"] == T0.isoformat() and r["symbol"] == "USDJPY" for r in rows)
    assert {r["timeframe"]: r["close"] for r in rows} == {"1h": 157.0, "4h": 157.2}


def test_snapshot_entries_skips_missing_closes() -> None:
    rows = ph.snapshot_entries({"USDJPY": {"1h": None, "4h": 157.2}}, now=T0)
    assert [r["timeframe"] for r in rows] == ["4h"]


def test_snapshot_entries_feed_into_series() -> None:
    """スナップショット行は build_close_series が読める形になっている。"""
    rows = ph.snapshot_entries({"USDJPY": {"1h": 157.0}}, now=T0)
    series = ph.build_close_series(rows)
    assert series[("USDJPY", "1h")] == [(T0, 157.0)]


# ------------------------------------------------- 5分密系列で15mが解決できる


def test_dense_5min_series_resolves_15m_future_close() -> None:
    """5分刻みの価格系列なら 15m の主ホライズン(15分)の将来価格が取れる。

    毎時刻みの系列では窓[9,21分]に点が無く解決できないが、5分刻みなら
    15分後ちょうどの点が入る。fx_tf_snapshot が供給する密系列の狙い。
    """
    dense = ph.build_close_series(
        ph.snapshot_entries({"USDJPY": {"15m": 150.0}}, now=T0)
        + ph.snapshot_entries({"USDJPY": {"15m": 150.05}}, now=T0 + timedelta(minutes=15))
    )[("USDJPY", "15m")]
    close = ph.future_close_from_series(dense, T0, horizon_hours=0.25, tolerance_hours=0.1)
    assert close == 150.05

    # 毎時刻みだと同じ 15m ホライズンは解決できない(窓外)
    hourly = ph.build_close_series(
        ph.snapshot_entries({"USDJPY": {"15m": 150.0}}, now=T0)
        + ph.snapshot_entries({"USDJPY": {"15m": 150.5}}, now=T0 + timedelta(hours=1))
    )[("USDJPY", "15m")]
    assert ph.future_close_from_series(hourly, T0, 0.25, 0.1) is None
