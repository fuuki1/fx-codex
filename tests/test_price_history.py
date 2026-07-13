"""時間足別ジャーナル + 将来価格調達(price_history)のテスト(ネットワーク不要)。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json

import pytest

from fx_intel import price_history as ph
from fx_intel.append_only import AppendOnlyWriteError
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
    assert row["direction"] == "long"
    assert row["action"] == "no_trade"
    assert row["tech_score"] == 0.5  # tf_score を tech_score キーで記録(learning 互換)
    assert row["rsi"] == 55.0
    assert row["close"] == 150.0


def test_append_timeframe_plans_appends(tmp_path) -> None:
    path = tmp_path / "journal.jsonl"
    append_timeframe_plans(path, [_plan("1h", 1.0, "long", 156.0)], now=T0)
    append_timeframe_plans(path, [_plan("1h", 1.0, "long", 156.3)], now=T0 + timedelta(hours=1))
    assert len(list(read_entries(path))) == 2


def test_timeframe_journal_retry_is_idempotent_and_conflict_fails(tmp_path) -> None:
    path = tmp_path / "journal.jsonl"
    first = _plan("1h", 1.0, "long", 156.0)
    append_timeframe_plans(
        path,
        [first],
        now=T0 + timedelta(minutes=4, seconds=50),
        run_slot=T0,
    )
    append_timeframe_plans(
        path,
        [first],
        now=T0 + timedelta(minutes=5, seconds=10),
        run_slot=T0,
    )
    assert len(list(read_entries(path))) == 1

    conflicting = _plan("1h", 1.0, "long", 157.0)
    with pytest.raises(AppendOnlyWriteError, match="conflicting append"):
        append_timeframe_plans(
            path,
            [conflicting],
            now=T0 + timedelta(minutes=5, seconds=20),
            run_slot=T0,
        )
    assert len(list(read_entries(path))) == 1

    append_timeframe_plans(path, [conflicting], now=T0 + timedelta(minutes=5, seconds=20))
    assert len(list(read_entries(path))) == 2


def test_timeframe_journal_rejects_naive_now_or_run_slot(tmp_path) -> None:
    path = tmp_path / "journal.jsonl"
    plan = _plan("1h", 1.0, "long", 156.0)

    with pytest.raises(ValueError, match="timezone-aware"):
        append_timeframe_plans(path, [plan], now=T0.replace(tzinfo=None))
    with pytest.raises(ValueError, match="timezone-aware"):
        append_timeframe_plans(path, [plan], now=T0, run_slot=T0.replace(tzinfo=None))


def test_timeframe_journal_refuses_to_append_over_legacy_unhashed_rows(tmp_path) -> None:
    path = tmp_path / "journal.jsonl"
    path.write_text(
        json.dumps(
            {
                "ts": T0.isoformat(),
                "symbol": "USDJPY",
                "timeframe": "1h",
                "direction": "long",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(AppendOnlyWriteError, match="requires migration"):
        append_timeframe_plans(path, [_plan("1h", 1.0, "long", 156.0)], now=T0)


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
    ]
    assert ph.build_close_series(entries) == {}


@pytest.mark.parametrize("timestamp", ["bad-ts", "2026-06-29T09:00:00"])
def test_build_close_series_rejects_invalid_or_naive_time(timestamp) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        ph.build_close_series(
            [{"ts": timestamp, "symbol": "USDJPY", "timeframe": "1h", "close": 156.0}]
        )


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


def test_snapshot_entries_preserves_ohlc_bid_ask_spread() -> None:
    rows = ph.snapshot_entries(
        {
            "USDJPY": {
                "1h": {
                    "close": 157.0,
                    "open": 156.8,
                    "high": 157.2,
                    "low": 156.7,
                    "bid": 156.99,
                    "ask": 157.01,
                    "spread": 0.02,
                }
            }
        },
        now=T0,
    )

    assert len(rows) == 1
    row = rows[0]
    assert {
        key: row[key]
        for key in (
            "ts",
            "symbol",
            "timeframe",
            "close",
            "open",
            "high",
            "low",
            "bid",
            "ask",
            "spread",
        )
    } == {
        "ts": T0.isoformat(),
        "symbol": "USDJPY",
        "timeframe": "1h",
        "close": 157.0,
        "open": 156.8,
        "high": 157.2,
        "low": 156.7,
        "bid": 156.99,
        "ask": 157.01,
        "spread": 0.02,
    }
    assert row["event_time"] is None
    assert row["source_time"] is None
    assert row["source_record_id"] is None
    assert row["available_time"] == row["ingested_time"] == T0.isoformat()
    assert "source_time_unavailable" in row["data_quality_flags"]
    assert "source_record_id_unavailable" in row["data_quality_flags"]
    assert row["ohlc_scope"] == "forming_bar_snapshot"
    assert row["schema_version"] == ph.SNAPSHOT_SCHEMA_VERSION
    assert len(row["content_hash"]) == 64
    assert "forming_bar_ohlc_not_post_prediction_interval" in row["data_quality_flags"]


def test_snapshot_entries_feed_into_series() -> None:
    """スナップショット行は build_close_series が読める形になっている。"""
    rows = ph.snapshot_entries({"USDJPY": {"1h": 157.0}}, now=T0)
    series = ph.build_close_series(rows)
    assert series[("USDJPY", "1h")] == [(T0, 157.0)]


def test_snapshot_append_is_idempotent_under_advisory_lock(tmp_path) -> None:
    path = tmp_path / "prices.jsonl"
    rows = ph.snapshot_entries(
        {"USDJPY": {"1h": 157.0}}, now=T0, run_id="run-1", writer_id="writer-1"
    )

    assert ph.append_snapshot_entries(path, rows) == 1
    assert ph.append_snapshot_entries(path, rows) == 0
    assert len(path.read_text(encoding="utf-8").splitlines()) == 1


def test_snapshot_retry_requires_identical_provenance_not_only_identical_quote(tmp_path) -> None:
    path = tmp_path / "prices.jsonl"
    first = ph.snapshot_entries(
        {"USDJPY": {"1h": 157.0}}, now=T0, run_id="run-1", writer_id="writer-1"
    )
    different_run = ph.snapshot_entries(
        {"USDJPY": {"1h": 157.0}}, now=T0, run_id="run-2", writer_id="writer-1"
    )
    ph.append_snapshot_entries(path, first)

    with pytest.raises(ph.PriceHistoryWriteError, match="duplicate writer"):
        ph.append_snapshot_entries(path, different_run)

    assert len(path.read_text(encoding="utf-8").splitlines()) == 1


def test_snapshot_append_rejects_conflicting_writer_in_same_capture_slot(tmp_path) -> None:
    path = tmp_path / "prices.jsonl"
    first = ph.snapshot_entries({"USDJPY": {"1h": 157.0}}, now=T0, writer_id="writer-a")
    conflicting = ph.snapshot_entries(
        {"USDJPY": {"1h": 157.1}},
        now=T0 + timedelta(seconds=30),
        writer_id="writer-b",
    )
    ph.append_snapshot_entries(path, first)

    with pytest.raises(ph.PriceHistoryWriteError, match="duplicate writer"):
        ph.append_snapshot_entries(path, conflicting)
    assert len(path.read_text(encoding="utf-8").splitlines()) == 1


def test_snapshot_append_rejects_capture_slot_that_does_not_match_timestamp(tmp_path) -> None:
    path = tmp_path / "prices.jsonl"
    row = ph.snapshot_entries(
        {"USDJPY": {"1h": 157.0}},
        now=T0,
        run_id="run",
        writer_id="writer",
    )[0]
    row["capture_slot"] = (T0 + timedelta(minutes=5)).isoformat()
    row["content_hash"] = ph._content_hash(row)

    with pytest.raises(ph.PriceHistoryWriteError, match="capture_slot_mismatch"):
        ph.append_snapshot_entries(path, [row])


def test_snapshot_append_rejects_stale_authoritative_source_by_timeframe(tmp_path) -> None:
    path = tmp_path / "prices.jsonl"
    rows = ph.snapshot_entries(
        {
            "USDJPY": {
                "15m": {
                    "close": 157.0,
                    "source_time": T0 - timedelta(hours=1),
                    "source_record_id": "provider:stale",
                }
            }
        },
        now=T0,
    )

    with pytest.raises(ph.PriceHistoryWriteError, match="authoritative_source_stale"):
        ph.append_snapshot_entries(path, rows)
    assert path.read_text(encoding="utf-8") == ""


def test_snapshot_append_rejects_second_writer_even_when_quote_matches(tmp_path) -> None:
    path = tmp_path / "prices.jsonl"
    first = ph.snapshot_entries({"USDJPY": {"1h": 157.0}}, now=T0, writer_id="writer-a")
    second_writer = ph.snapshot_entries(
        {"USDJPY": {"1h": 157.0}},
        now=T0 + timedelta(seconds=30),
        writer_id="writer-b",
    )
    ph.append_snapshot_entries(path, first)

    with pytest.raises(ph.PriceHistoryWriteError, match="duplicate writer"):
        ph.append_snapshot_entries(path, second_writer)
    assert len(path.read_text(encoding="utf-8").splitlines()) == 1


def test_snapshot_append_rejects_tampered_pending_content(tmp_path) -> None:
    path = tmp_path / "prices.jsonl"
    row = ph.snapshot_entries({"USDJPY": {"1h": 157.0}}, now=T0)[0]
    row["close"] = 158.0

    with pytest.raises(ph.PriceHistoryWriteError, match="content_hash mismatch"):
        ph.append_snapshot_entries(path, [row])

    assert path.read_text(encoding="utf-8") == ""


def test_snapshot_append_rejects_tampered_existing_content_before_append(tmp_path) -> None:
    path = tmp_path / "prices.jsonl"
    original = ph.snapshot_entries({"USDJPY": {"1h": 157.0}}, now=T0)
    ph.append_snapshot_entries(path, original)
    tampered = json.loads(path.read_text(encoding="utf-8"))
    tampered["close"] = 158.0
    path.write_text(json.dumps(tampered) + "\n", encoding="utf-8")
    later = ph.snapshot_entries({"USDJPY": {"1h": 157.1}}, now=T0 + timedelta(minutes=5))

    with pytest.raises(ph.PriceHistoryWriteError, match="content_hash mismatch"):
        ph.append_snapshot_entries(path, later)

    assert len(path.read_text(encoding="utf-8").splitlines()) == 1


def test_snapshot_append_rejects_unverifiable_legacy_existing_content(tmp_path) -> None:
    path = tmp_path / "prices.jsonl"
    legacy = {
        "ts": T0.isoformat(),
        "symbol": "USDJPY",
        "timeframe": "1h",
        "close": 157.0,
    }
    path.write_text(json.dumps(legacy) + "\n", encoding="utf-8")
    current = ph.snapshot_entries({"USDJPY": {"1h": 157.1}}, now=T0 + timedelta(minutes=5))

    with pytest.raises(ph.PriceHistoryWriteError, match="invalid snapshot content_hash"):
        ph.append_snapshot_entries(path, current)

    assert json.loads(path.read_text(encoding="utf-8")) == legacy


def test_snapshot_reader_requires_verified_v2_and_respects_as_of(tmp_path) -> None:
    path = tmp_path / "prices.jsonl"
    rows = ph.snapshot_entries(
        {"USDJPY": {"1h": 157.0}}, now=T0, run_id="run-1", writer_id="writer-1"
    )
    ph.append_snapshot_entries(path, rows)

    assert list(ph.read_snapshot_entries(path, as_of=T0)) == rows
    with pytest.raises(ph.PriceHistoryReadError, match="future price row"):
        list(ph.read_snapshot_entries(path, as_of=T0 - timedelta(seconds=1)))


def test_snapshot_reader_and_writer_reject_duplicate_existing_natural_key(tmp_path) -> None:
    path = tmp_path / "prices.jsonl"
    rows = ph.snapshot_entries(
        {"USDJPY": {"1h": 157.0}}, now=T0, run_id="run-1", writer_id="writer-1"
    )
    ph.append_snapshot_entries(path, rows)
    original_line = path.read_text(encoding="utf-8")
    with path.open("a", encoding="utf-8") as handle:
        handle.write(original_line)

    with pytest.raises(ph.PriceHistoryReadError, match="duplicate price snapshot natural key"):
        list(ph.read_snapshot_entries(path, as_of=T0))
    later = ph.snapshot_entries({"USDJPY": {"1h": 157.1}}, now=T0 + timedelta(minutes=5))
    with pytest.raises(ph.PriceHistoryWriteError, match="duplicate existing snapshots"):
        ph.append_snapshot_entries(path, later)


def test_snapshot_reader_and_writer_reject_timestamp_regression(tmp_path) -> None:
    path = tmp_path / "prices.jsonl"
    current = ph.snapshot_entries(
        {"USDJPY": {"1h": 157.0}}, now=T0, run_id="run-1", writer_id="writer-1"
    )
    older = ph.snapshot_entries(
        {"EURUSD": {"1h": 1.15}},
        now=T0 - timedelta(minutes=5),
        run_id="run-older",
        writer_id="writer-1",
    )
    ph.append_snapshot_entries(path, current)

    with pytest.raises(ph.PriceHistoryWriteError, match="pending snapshot ts"):
        ph.append_snapshot_entries(path, older)

    older_line = json.dumps(older[0], ensure_ascii=False, sort_keys=True) + "\n"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(older_line)
    with pytest.raises(ph.PriceHistoryReadError, match="snapshot ts is not monotonic"):
        list(ph.read_snapshot_entries(path, as_of=T0))


def test_snapshot_reader_and_writer_reject_causal_clock_regression(tmp_path) -> None:
    path = tmp_path / "prices.jsonl"
    current = ph.snapshot_entries(
        {"USDJPY": {"1h": 157.0}}, now=T0, run_id="run-1", writer_id="writer-1"
    )
    regressed = ph.snapshot_entries(
        {"EURUSD": {"1h": 1.15}},
        now=T0 + timedelta(minutes=5),
        run_id="run-2",
        writer_id="writer-1",
    )
    for field in ("available_time", "ingested_time"):
        regressed[0][field] = (T0 - timedelta(hours=1)).isoformat()
    regressed[0]["content_hash"] = ph._content_hash(regressed[0])
    ph.append_snapshot_entries(path, current)

    with pytest.raises(ph.PriceHistoryWriteError, match="pending snapshot available_time"):
        ph.append_snapshot_entries(path, regressed)

    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(regressed[0], ensure_ascii=False, sort_keys=True) + "\n")
    with pytest.raises(ph.PriceHistoryReadError, match="snapshot available_time is not monotonic"):
        list(ph.read_snapshot_entries(path, as_of=T0 + timedelta(minutes=6)))


def test_snapshot_reader_rejects_legacy_tamper_and_naive_time(tmp_path) -> None:
    path = tmp_path / "prices.jsonl"
    legacy = {"ts": T0.isoformat(), "symbol": "USDJPY", "timeframe": "1h", "close": 157.0}
    path.write_text(json.dumps(legacy) + "\n", encoding="utf-8")
    with pytest.raises(ph.PriceHistoryReadError, match="content_hash"):
        list(ph.read_snapshot_entries(path, as_of=T0))

    row = ph.snapshot_entries(
        {"USDJPY": {"1h": 157.0}}, now=T0, run_id="run-1", writer_id="writer-1"
    )[0]
    row["ts"] = "2026-06-29T09:00:00"
    row["content_hash"] = ph._content_hash(row)
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    with pytest.raises(ph.PriceHistoryReadError, match="ts_invalid"):
        list(ph.read_snapshot_entries(path, as_of=T0))

    row = ph.snapshot_entries(
        {"USDJPY": {"1h": 157.0}}, now=T0, run_id="run-1", writer_id="writer-1"
    )[0]
    row["close"] = 999.0
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    with pytest.raises(ph.PriceHistoryReadError, match="content_hash mismatch"):
        list(ph.read_snapshot_entries(path, as_of=T0))


def test_snapshot_entries_reject_naive_timestamp() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        ph.snapshot_entries(
            {"USDJPY": {"1h": 157.0}},
            now=datetime(2026, 6, 22, 8, 0),
        )


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
