"""Dukascopy tick → 確定足アダプタのテスト。

守るべき性質は3つで、どれも壊れると採点が黙って誤る。
1. 形成中バーを書かない(未来情報の混入を防ぐ)
2. 採点が信頼するスコープを名乗り、high/low が実際に採用される
3. 既存の 15m/1h/4h/1d 行と自然キーで衝突しない
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json

from fx_intel import trade_outcome
from fx_intel.price_history import append_snapshot_entries, snapshot_entries
from fx_intel.price_path_adapter import (
    ADAPTER_OHLC_SCOPE,
    ADAPTER_TIMEFRAME,
    BAR_SECONDS,
    build_closed_bars,
    build_rows,
    iter_ticks,
    load_quotes,
    parse_tick,
)

BASE = datetime(2026, 7, 28, 2, 0, 0, tzinfo=UTC)


def _tick(
    offset_seconds: float,
    mid: float,
    *,
    symbol: str = "USDJPY",
    quality: str = "usable",
    bid: float | None = None,
    ask: float | None = None,
) -> dict:
    event = BASE + timedelta(seconds=offset_seconds)
    return {
        "instrument": symbol,
        "quality_state": quality,
        "provider_event_time": event.isoformat(),
        "received_at": (event + timedelta(hours=2)).isoformat(),
        "mid": mid,
        "bid": bid if bid is not None else mid - 0.001,
        "ask": ask if ask is not None else mid + 0.001,
    }


def _ticks(payloads: list[dict]):
    return list(iter_ticks(json.dumps(p, ensure_ascii=False) for p in payloads))


def test_forming_bar_is_never_emitted() -> None:
    """期間が終わっていないバーは出力しない(PIT保護の本体)。"""
    ticks = _ticks([_tick(0, 150.0), _tick(60, 150.5), _tick(120, 149.5)])

    # バー終端(02:05)より前を as_of にすると、まだ確定していない
    mid_bar = build_closed_bars(ticks, as_of=BASE + timedelta(seconds=120))
    assert mid_bar == []

    # 終端ちょうどで確定する
    closed = build_closed_bars(ticks, as_of=BASE + timedelta(seconds=BAR_SECONDS))
    assert len(closed) == 1
    assert closed[0].bar_end == BASE + timedelta(seconds=BAR_SECONDS)


def test_high_low_come_from_observed_ticks_only() -> None:
    ticks = _ticks([_tick(0, 150.0), _tick(60, 151.0), _tick(120, 149.0), _tick(180, 150.4)])
    bar = build_closed_bars(ticks, as_of=BASE + timedelta(hours=1))[0]

    assert bar.open == 150.0
    assert bar.high == 151.0
    assert bar.low == 149.0
    assert bar.close == 150.4
    assert bar.tick_count == 4


def test_unusable_ticks_are_dropped() -> None:
    """collector が usable と判定していない行は再解釈せず捨てる。"""
    assert parse_tick(_tick(0, 150.0, quality="quarantined")) is None
    assert parse_tick(_tick(0, 150.0, quality="")) is None

    ticks = _ticks([_tick(0, 150.0), _tick(60, 999.0, quality="rejected")])
    bar = build_closed_bars(ticks, as_of=BASE + timedelta(hours=1))[0]
    assert bar.high == 150.0, "捨てたはずの tick が high を汚染している"


def test_row_declares_a_scope_the_scorer_trusts() -> None:
    """採点が high/low を採用する条件を満たすこと。

    これが崩れると close_only_path に落ち、TP/SL 判定が沈黙して壊れる。
    """
    ticks = _ticks([_tick(0, 150.0), _tick(60, 151.0)])
    bar = build_closed_bars(ticks, as_of=BASE + timedelta(hours=1))[0]
    row = bar.to_row(run_id="r", writer_id="w")

    assert row["ohlc_scope"] == ADAPTER_OHLC_SCOPE
    assert ADAPTER_OHLC_SCOPE in trade_outcome.TRUSTED_POST_PREDICTION_OHLC_SCOPES

    point = trade_outcome._price_path_point(bar.bar_end, row)
    assert point is not None
    assert point.rejected_range is False
    assert point.high == 151.0
    assert point.low == 150.0


def test_event_time_and_available_time_are_separated() -> None:
    """成立時刻と観測可能時刻を混同しない。"""
    ticks = _ticks([_tick(0, 150.0), _tick(60, 150.2)])
    bar = build_closed_bars(ticks, as_of=BASE + timedelta(hours=1))[0]
    row = bar.to_row(run_id="r", writer_id="w")

    assert row["event_time"] == bar.bar_end.isoformat()
    # received_at は provider_event_time + 2h(lag)なので必ず後になる
    assert row["available_time"] > row["event_time"]


def test_does_not_collide_with_existing_timeframe_rows(tmp_path) -> None:
    """既存の 15m 行と同じ capture_slot でも writer が拒否しないこと。

    同じ timeframe 名を使うと自然キーが衝突し、writer が例外で落ちて
    実機の収集を止める。専用 timeframe 名がその防波堤になっている。
    """
    prices = tmp_path / "prices.jsonl"
    # 既存経路(5分スナップショット writer)が書いた 15m 行。
    # tick と同じ 5 分スロットに落ちるので、自然キーが重なる条件を作っている。
    existing = snapshot_entries(
        {"USDJPY": {"15m": {"close": 999.0, "high": 999.5, "low": 998.5}}},
        now=BASE,
    )
    append_snapshot_entries(prices, existing)

    ticks = _ticks([_tick(0, 150.0), _tick(60, 151.0)])
    bars = build_closed_bars(ticks, as_of=BASE + timedelta(hours=1))
    rows = build_rows(bars, run_id="r", writer_id="w")
    appended = append_snapshot_entries(prices, rows)

    assert appended == 1
    written = [json.loads(line) for line in prices.read_text().splitlines() if line.strip()]
    timeframes = {row["timeframe"] for row in written}
    assert timeframes == {"15m", ADAPTER_TIMEFRAME}
    # 既存行は書き換えられていない
    assert any(row["timeframe"] == "15m" and row["close"] == 999.0 for row in written)


def test_replay_is_idempotent(tmp_path) -> None:
    """同じ tick を再処理しても二重に増えない(再実行安全)。"""
    prices = tmp_path / "prices.jsonl"
    ticks = _ticks([_tick(0, 150.0), _tick(60, 151.0)])
    bars = build_closed_bars(ticks, as_of=BASE + timedelta(hours=1))

    first = append_snapshot_entries(prices, build_rows(bars, run_id="r", writer_id="w"))
    second = append_snapshot_entries(prices, build_rows(bars, run_id="r", writer_id="w"))

    assert (first, second) == (1, 0)


def test_bars_split_per_symbol_and_slot() -> None:
    ticks = _ticks(
        [
            _tick(0, 150.0),
            _tick(60, 150.5),
            _tick(BAR_SECONDS, 151.0),  # 次のスロット
            _tick(0, 1.10, symbol="EURUSD"),
        ]
    )
    bars = build_closed_bars(ticks, as_of=BASE + timedelta(hours=1))

    keys = {(bar.symbol, bar.bar_start) for bar in bars}
    assert len(keys) == 3
    assert ("EURUSD", BASE) in keys


def test_bid_ask_absence_is_flagged_not_faked() -> None:
    payload = _tick(0, 150.0)
    payload["bid"] = None
    payload["ask"] = None
    ticks = _ticks([payload])
    bar = build_closed_bars(ticks, as_of=BASE + timedelta(hours=1))[0]
    row = bar.to_row(run_id="r", writer_id="w")

    assert "bid" not in row and "ask" not in row
    assert "bid_ask_unavailable" in row["data_quality_flags"]


def test_load_quotes_missing_file_is_not_an_error(tmp_path) -> None:
    """collector 未稼働は異常ではない(空で返す)。"""
    assert load_quotes(tmp_path / "nope.jsonl") == []


def test_truncated_trailing_line_does_not_lose_earlier_ticks() -> None:
    """追記中のファイルを読んでも、完全な行までは活かす。"""
    good = json.dumps(_tick(0, 150.0), ensure_ascii=False)
    ticks = list(iter_ticks([good, '{"instrument": "USDJPY", "quality_st']))
    assert len(ticks) == 1
