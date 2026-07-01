from __future__ import annotations

from datetime import UTC, datetime

import pytest
from domain import (
    SignalError,
    normalize_signal,
    parse_ts,
    rate_limit_allow,
    signal_is_stale,
    within_session,
)

UTC = UTC


def dt(y: int, mo: int, d: int, h: int, mi: int = 0) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=UTC)


# ---- normalize -------------------------------------------------------------
def test_normalize_valid():
    sig = normalize_signal({"symbol": "usdjpy", "side": "buy", "qty": 1000, "type": "market", "id": "abc"})
    assert sig["symbol"] == "USDJPY"
    assert sig["side"] == "BUY"
    assert sig["type"] == "MARKET"
    assert sig["asset"] == "fx"
    assert sig["qty"] == 1000.0
    assert sig["idem"] == "abc"


def test_normalize_side_alias():
    assert normalize_signal({"symbol": "AAPL", "side": "long", "qty": 1})["side"] == "BUY"
    assert normalize_signal({"symbol": "AAPL", "side": "SHORT", "qty": 1})["side"] == "SELL"


def test_normalize_invalid_side():
    with pytest.raises(SignalError):
        normalize_signal({"symbol": "USDJPY", "side": "hold", "qty": 1})


def test_normalize_missing_symbol():
    with pytest.raises(SignalError):
        normalize_signal({"side": "buy", "qty": 1})


def test_normalize_bad_qty():
    with pytest.raises(SignalError):
        normalize_signal({"symbol": "USDJPY", "side": "buy", "qty": 0})


def test_normalize_carries_stop_tp_reason():
    sig = normalize_signal({
        "symbol": "USDJPY", "side": "buy", "qty": 1000,
        "stop_distance": 0.3, "tp_distance": 0.9, "reason": "trend continuation",
    })
    assert sig["stop_distance"] == 0.3
    assert sig["tp_distance"] == 0.9
    assert sig["reason"] == "trend continuation"


def test_normalize_derives_distances_from_prices():
    # LIMIT で entry=price、stop_price/take_profit から距離を導出
    sig = normalize_signal({
        "symbol": "USDJPY", "side": "buy", "qty": 1000, "type": "limit", "price": 150.0,
        "stop_price": 149.7, "take_profit": 150.9,
    })
    assert sig["stop_distance"] == pytest.approx(0.3)
    assert sig["tp_distance"] == pytest.approx(0.9)


def test_normalize_no_risk_fields_are_none():
    sig = normalize_signal({"symbol": "USDJPY", "side": "buy", "qty": 1})
    assert sig["stop_distance"] is None
    assert sig["tp_distance"] is None
    assert sig["reason"] is None
    assert sig["intent"] == "entry"          # 未指定は新規扱い


def test_normalize_intent_exit_passthrough():
    # TradingView からも intent=exit で手仕舞いを指示できる（close/flat も同義）
    assert normalize_signal({"symbol": "USDJPY", "side": "sell", "qty": 1, "intent": "exit"})["intent"] == "exit"
    assert normalize_signal({"symbol": "USDJPY", "side": "sell", "qty": 1, "intent": "CLOSE"})["intent"] == "close"
    assert normalize_signal({"symbol": "USDJPY", "side": "sell", "qty": 1, "intent": "weird"})["intent"] == "entry"


def test_normalize_limit_requires_price():
    with pytest.raises(SignalError):
        normalize_signal({"symbol": "USDJPY", "side": "buy", "qty": 1, "type": "limit"})
    sig = normalize_signal({"symbol": "USDJPY", "side": "buy", "qty": 1, "type": "limit", "price": 150})
    assert sig["price"] == 150.0


def test_idem_deterministic_and_content_based():
    a = normalize_signal({"symbol": "USDJPY", "side": "buy", "qty": 1, "type": "market"})
    b = normalize_signal({"symbol": "USDJPY", "side": "buy", "qty": 1, "type": "market"})
    assert a["idem"] == b["idem"]
    assert a["idem"].startswith("sha256:")


# ---- parse_ts / staleness --------------------------------------------------
def test_parse_ts_epoch_seconds():
    assert parse_ts(1_700_000_000) == 1_700_000_000.0
    assert parse_ts("1700000000") == 1_700_000_000.0


def test_parse_ts_epoch_millis():
    # ミリ秒 epoch は秒へ補正される
    assert parse_ts(1_700_000_000_000) == 1_700_000_000.0


def test_parse_ts_iso8601_tradingview_timenow():
    # TradingView {{timenow}} 形式（末尾 Z）
    assert parse_ts("2024-01-01T00:00:00Z") == datetime(2024, 1, 1, tzinfo=UTC).timestamp()


def test_parse_ts_unparseable_falls_back_to_now():
    assert parse_ts("not-a-time", now=123.0) == 123.0
    assert parse_ts(None, now=123.0) == 123.0
    assert parse_ts(True, now=123.0) == 123.0  # bool を 1.0 と誤解しない


def test_normalize_accepts_iso_ts_without_crashing():
    # 以前は float("2024-..") で 500 になり得た回帰の防止
    sig = normalize_signal(
        {"symbol": "USDJPY", "side": "buy", "qty": 1, "timenow": "2024-01-01T00:00:00Z"}
    )
    assert sig["ts"] == datetime(2024, 1, 1, tzinfo=UTC).timestamp()


def test_signal_is_stale():
    now = 1000.0
    assert signal_is_stale(now - 200, now, 60) is True       # 古すぎ
    assert signal_is_stale(now + 200, now, 60) is True       # 未来すぎ
    assert signal_is_stale(now - 30, now, 60) is False       # 範囲内
    assert signal_is_stale(now - 99999, now, 0) is False     # 0 で無効


# ---- within_session --------------------------------------------------------
def test_fx_weekend_closed():
    assert within_session("fx", "USDJPY", dt(2024, 1, 6, 12)) is False  # Saturday


def test_fx_weekday_open():
    assert within_session("fx", "USDJPY", dt(2024, 1, 3, 12)) is True  # Wednesday


def test_fx_friday_close_boundary():
    assert within_session("fx", "USDJPY", dt(2024, 1, 5, 22)) is False  # Fri 22:00 UTC
    assert within_session("fx", "USDJPY", dt(2024, 1, 5, 20)) is True


def test_jp_equity_hours():
    assert within_session("jp_stock", "7203", dt(2024, 1, 4, 1)) is True   # 10:00 JST
    assert within_session("jp_stock", "7203", dt(2024, 1, 4, 3)) is False  # 12:00 JST lunch


def test_us_equity_hours():
    assert within_session("us_stock", "AAPL", dt(2024, 1, 4, 15)) is True   # 10:00 ET
    assert within_session("us_stock", "AAPL", dt(2024, 1, 4, 22)) is False  # 17:00 ET


# ---- rate limit ------------------------------------------------------------
def test_rate_limit_sliding_window(fake_redis):
    now = 1000.0
    assert rate_limit_allow(fake_redis, "k", 2, now=now) is True
    assert rate_limit_allow(fake_redis, "k", 2, now=now) is True
    assert rate_limit_allow(fake_redis, "k", 2, now=now) is False
    # ウィンドウが過ぎれば再び許可
    assert rate_limit_allow(fake_redis, "k", 2, now=now + 61) is True
