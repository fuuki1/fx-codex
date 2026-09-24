"""時間足別ジャーナル + 将来価格調達(price_history)のテスト(ネットワーク不要)。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json

import pytest

from fx_intel import price_history as ph
from fx_intel.journal import (
    DECISION_JOURNAL_PIT_CONTRACT,
    PointInTimeError,
    append_timeframe_plans,
    is_pit_eligible_entry,
    read_entries,
)
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


def _pit_ready_plan() -> TimeframePlan:
    plan = _plan("1h", 1.0, "long", 156.0)
    plan.input_context_id = "context-1"
    plan.input_context = {"liquidity": {"quote": {"source_record_id": "USDJPY:quote:1"}}}
    return plan


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


def test_append_timeframe_plans_persists_canonical_label_metadata(tmp_path) -> None:
    path = tmp_path / "journal.jsonl"
    plan = _plan("1h", 1.0, "long", 156.0)
    plan.planned_risk_distance = 0.25
    plan.quote_available_at = "2026-06-29T08:59:59+00:00"
    plan.quote_source = "fixture_quotes"
    plan.quote_source_record_id = "quote-123"

    append_timeframe_plans(path, [plan], now=T0)

    row = next(read_entries(path))
    assert row["label_version"] == plan.label_version
    assert row["label_provenance"] == plan.label_provenance
    assert row["planned_risk_distance"] == 0.25
    assert row["quote_available_at"] == plan.quote_available_at
    assert row["quote_source"] == "fixture_quotes"
    assert row["quote_source_record_id"] == "quote-123"
    assert row["cost_status"] == plan.cost_status
    assert row["pre_guard_direction"] == plan.pre_guard_direction
    assert row["pre_guard_execution_snapshot"] == plan.pre_guard_execution_snapshot


def test_append_timeframe_plans_appends(tmp_path) -> None:
    path = tmp_path / "journal.jsonl"
    append_timeframe_plans(path, [_plan("1h", 1.0, "long", 156.0)], now=T0)
    append_timeframe_plans(path, [_plan("1h", 1.0, "long", 156.3)], now=T0 + timedelta(hours=1))
    assert len(list(read_entries(path))) == 2


def test_append_timeframe_plans_enforces_full_pit_contract(tmp_path) -> None:
    path = tmp_path / "journal.jsonl"
    source_cutoff = T0 - timedelta(minutes=2)
    feature_available = T0 - timedelta(seconds=1)

    append_timeframe_plans(
        path,
        [_pit_ready_plan()],
        now=T0,
        source_cutoff=source_cutoff,
        max_feature_available_time=feature_available,
        decision_ids=["decision-1"],
    )

    row = next(read_entries(path))
    assert row["prediction_time"] == T0.isoformat()
    assert row["source_cutoff"] == source_cutoff.isoformat()
    assert row["max_feature_available_time"] == feature_available.isoformat()
    assert row["pit_contract"] == DECISION_JOURNAL_PIT_CONTRACT
    assert row["decision_id"] == "decision-1"
    assert row["mode"] == "per_timeframe"
    assert row["producer"] == "timeframe_raw"
    assert row["source_record_ids"] == ["USDJPY:quote:1"]
    assert is_pit_eligible_entry(row)


def test_append_timeframe_plans_keeps_incomplete_rows_diagnostic_only(tmp_path) -> None:
    path = tmp_path / "journal.jsonl"
    append_timeframe_plans(path, [_plan("1h", 1.0, "long", 156.0)], now=T0)

    row = next(read_entries(path))
    assert row["pit_eligible"] is False
    assert row["decision_id"] is None
    assert row["source_cutoff"] is None
    assert row["max_feature_available_time"] is None
    assert not is_pit_eligible_entry(row)


def test_append_timeframe_plans_rejects_naive_or_future_times(tmp_path) -> None:
    path = tmp_path / "journal.jsonl"
    with pytest.raises(PointInTimeError, match="timezone-aware"):
        append_timeframe_plans(
            path,
            [_pit_ready_plan()],
            now=T0.replace(tzinfo=None),
            source_cutoff=T0 - timedelta(minutes=2),
            max_feature_available_time=T0 - timedelta(seconds=1),
            decision_ids=["decision-1"],
        )

    with pytest.raises(PointInTimeError, match="PIT ordering"):
        append_timeframe_plans(
            path,
            [_pit_ready_plan()],
            now=T0,
            source_cutoff=T0 - timedelta(minutes=2),
            max_feature_available_time=T0 + timedelta(seconds=1),
            decision_ids=["decision-1"],
        )


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
    assert row["event_time"] == row["available_time"] == row["ingested_time"]
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


def test_snapshot_entries_reject_naive_timestamp() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        ph.snapshot_entries(
            {"USDJPY": {"1h": 157.0}},
            now=datetime(2026, 6, 22, 8, 0),
        )


def test_pit_snapshot_entry_rejects_future_and_hash_mismatch() -> None:
    row = ph.snapshot_entries({"USDJPY": {"1h": 157.0}}, now=T0)[0]
    assert ph.is_pit_eligible_snapshot_entry(row, as_of=T0)
    assert not ph.is_pit_eligible_snapshot_entry(row, as_of=T0 - timedelta(seconds=1))

    changed = {**row, "close": 999.0}
    assert not ph.is_pit_eligible_snapshot_entry(changed, as_of=T0)


def test_read_recent_snapshot_entries_is_byte_bounded(tmp_path) -> None:
    path = tmp_path / "prices.jsonl"
    rows = ph.snapshot_entries({"USDJPY": {"15m": 150.0, "1h": 151.0, "4h": 152.0}}, now=T0)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    loaded = ph.read_recent_snapshot_entries(path, max_bytes=1200)

    assert 0 < len(loaded) < len(rows)
    assert loaded[-1]["timeframe"] == "4h"


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


# --- snapshot key index(書き手が全行を LOCK_EX 下で読み直すのをやめる) -----------

import shutil as _shutil  # noqa: E402

from fx_intel.price_path_adapter import ClosedBar, build_rows  # noqa: E402

_KI_BASE = datetime(2026, 9, 15, 9, 0, tzinfo=UTC)


def _ki_bar(minutes: int, close: float, *, symbol: str = "EURUSD") -> dict:
    start = _KI_BASE + timedelta(minutes=minutes)
    bar = ClosedBar(
        symbol=symbol,
        bar_start=start,
        bar_end=start + timedelta(minutes=5),
        open=close,
        high=close + 0.001,
        low=close - 0.001,
        close=close,
        bid=close - 0.00002,
        ask=close + 0.00002,
        tick_count=3,
        available_time=start + timedelta(hours=2),
    )
    return build_rows([bar], run_id=f"ki-{minutes}")[0]


def _ki_snapshot(minutes: int, close: float) -> dict:
    return ph.snapshot_entries(
        {"USDJPY": {"15m": {"close": close, "high": close + 0.1, "low": close - 0.1}}},
        now=_KI_BASE + timedelta(minutes=minutes),
        run_id=f"snap-{minutes}",
        writer_id="snapshot-writer",
    )[0]


def _ki_batches() -> list[list[dict]]:
    return [
        [_ki_bar(0, 1.15), _ki_bar(5, 1.16), _ki_snapshot(0, 150.0)],
        [_ki_bar(5, 1.16), _ki_bar(10, 1.17)],  # 1 duplicate, 1 new
        [_ki_snapshot(5, 150.2), _ki_bar(15, 1.18, symbol="USDJPY")],
    ]


def test_key_index_append_writes_identical_bytes_to_full_scan(tmp_path) -> None:
    plain = tmp_path / "plain.jsonl"
    indexed = tmp_path / "indexed.jsonl"
    counts_plain = [ph.append_snapshot_entries(plain, batch) for batch in _ki_batches()]
    counts_indexed = [
        ph.append_snapshot_entries(indexed, batch, key_index=ph.snapshot_key_index_path(indexed))
        for batch in _ki_batches()
    ]

    assert counts_indexed == counts_plain == [3, 1, 2]
    assert indexed.read_bytes() == plain.read_bytes()


def test_key_index_reads_only_the_appended_tail(tmp_path, monkeypatch) -> None:
    path = tmp_path / "prices.jsonl"
    index = ph.snapshot_key_index_path(path)
    ph.append_snapshot_entries(
        path, [_ki_bar(m, 1.1 + m / 1000) for m in range(0, 500, 5)], key_index=index
    )
    size_before = path.stat().st_size
    read_bytes: list[int] = []
    original = ph._SnapshotKeyIndex._trusted_offset

    def spy(self, handle):
        offset = original(self, handle)
        read_bytes.append(size_before - offset)
        return offset

    monkeypatch.setattr(ph._SnapshotKeyIndex, "_trusted_offset", spy)
    ph.append_snapshot_entries(path, [_ki_bar(600, 1.5)], key_index=index)

    assert read_bytes[0] == 0  # 既存 100 行は読み直さない


def test_key_index_sees_rows_appended_by_a_writer_without_the_index(tmp_path) -> None:
    path = tmp_path / "prices.jsonl"
    index = ph.snapshot_key_index_path(path)
    ph.append_snapshot_entries(path, [_ki_bar(0, 1.15)], key_index=index)
    ph.append_snapshot_entries(path, [_ki_bar(5, 1.16)])  # 旧 writer(索引を更新しない)

    with pytest.raises(ph.PriceHistoryWriteError, match="duplicate writer"):
        ph.append_snapshot_entries(path, [_ki_bar(5, 1.99)], key_index=index)
    assert ph.append_snapshot_entries(path, [_ki_bar(5, 1.16)], key_index=index) == 0


def test_key_index_detects_conflicts_already_inside_the_file(tmp_path) -> None:
    path = tmp_path / "prices.jsonl"
    rows = [_ki_bar(0, 1.15), _ki_bar(0, 1.99)]
    path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))
    with pytest.raises(ph.PriceHistoryWriteError, match="conflicting existing snapshots"):
        ph.append_snapshot_entries(
            path, [_ki_bar(5, 1.2)], key_index=ph.snapshot_key_index_path(path)
        )


def test_key_index_refuses_to_append_after_an_incomplete_trailing_line(tmp_path) -> None:
    path = tmp_path / "prices.jsonl"
    path.write_text(json.dumps(_ki_bar(0, 1.15), sort_keys=True))  # 改行なし
    with pytest.raises(ph.PriceHistoryWriteError, match="incomplete trailing"):
        ph.append_snapshot_entries(
            path, [_ki_bar(5, 1.2)], key_index=ph.snapshot_key_index_path(path)
        )


def test_key_index_raises_the_same_conflict_as_full_scan(tmp_path) -> None:
    for mode in ("plain", "indexed"):
        path = tmp_path / f"{mode}.jsonl"
        kwargs = {"key_index": ph.snapshot_key_index_path(path)} if mode == "indexed" else {}
        ph.append_snapshot_entries(path, [_ki_bar(0, 1.15), _ki_bar(5, 1.16)], **kwargs)
        before = path.read_bytes()
        with pytest.raises(ph.PriceHistoryWriteError, match="duplicate writer"):
            ph.append_snapshot_entries(path, [_ki_bar(0, 1.99)], **kwargs)
        assert path.read_bytes() == before


@pytest.mark.parametrize("change", ["replaced", "truncated", "last_line_rewritten"])
def test_key_index_rebuilds_when_the_file_is_not_the_indexed_one(tmp_path, change) -> None:
    path = tmp_path / "prices.jsonl"
    index = ph.snapshot_key_index_path(path)
    ph.append_snapshot_entries(path, [_ki_bar(0, 1.15), _ki_bar(5, 1.16)], key_index=index)
    lines = path.read_bytes().splitlines(keepends=True)
    if change == "replaced":
        copy = tmp_path / "copy.jsonl"
        _shutil.copyfile(path, copy)
        copy.replace(path)
    elif change == "truncated":
        path.write_bytes(lines[0])
    else:
        row = json.loads(lines[1])
        row["close"] = 1.99
        path.write_bytes(lines[0] + (json.dumps(row, sort_keys=True) + "\n").encode())

    if change == "truncated":
        assert ph.append_snapshot_entries(path, [_ki_bar(5, 1.50)], key_index=index) == 1
    else:
        with pytest.raises(ph.PriceHistoryWriteError, match="duplicate writer"):
            ph.append_snapshot_entries(path, [_ki_bar(5, 1.50)], key_index=index)
        current = json.loads(path.read_bytes().splitlines()[1])
        assert current["close"] == (1.16 if change == "replaced" else 1.99)
        assert ph.append_snapshot_entries(path, [current], key_index=index) == 0


def test_snapshot_writer_uses_the_key_index(tmp_path) -> None:
    import fx_tf_snapshot

    path = tmp_path / "briefing_tf_prices.jsonl"
    rows = [_ki_snapshot(0, 150.0), _ki_snapshot(5, 150.1)]
    fx_tf_snapshot.append_snapshot(path, rows)
    fx_tf_snapshot.append_snapshot(path, rows)

    assert ph.snapshot_key_index_path(path).is_file()
    assert len(path.read_text().splitlines()) == 2
