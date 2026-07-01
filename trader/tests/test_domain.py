from __future__ import annotations

from datetime import UTC, datetime

import pytest
from domain import SignalError, normalize_signal, rate_limit_allow, within_session

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


# ---- within_session holiday calendar ---------------------------------------
def test_holidays_ignored_when_not_provided():
    # 2024-01-08 10:00 JST は平日だが holidays 省略時は従来通り祝日を考慮しない
    assert within_session("jp_stock", "7203", dt(2024, 1, 8, 1)) is True


def test_jp_equity_closed_on_holiday():
    holidays = {"jp_stock": {"2024-01-08"}}  # 成人の日（月曜）
    assert within_session("jp_stock", "7203", dt(2024, 1, 8, 1), holidays=holidays) is False
    # 前日（休日ではない）は通常営業
    assert within_session("jp_stock", "7203", dt(2024, 1, 5, 1), holidays=holidays) is True


def test_us_equity_closed_on_holiday():
    holidays = {"us_stock": {"2025-01-20"}}  # MLK Day（月曜）
    assert within_session("us_stock", "AAPL", dt(2025, 1, 20, 15), holidays=holidays) is False


def test_fx_closed_on_holiday_even_on_weekday():
    holidays = {"fx": {"2024-12-25"}}  # クリスマス（水曜）
    assert within_session("fx", "USDJPY", dt(2024, 12, 25, 12), holidays=holidays) is False
    assert within_session("fx", "USDJPY", dt(2024, 12, 24, 12), holidays=holidays) is True


def test_holiday_calendar_is_per_venue():
    # symbol が数字なら jp_stock 判定になるため us_stock の休日は影響しない
    holidays = {"us_stock": {"2024-01-08"}}
    assert within_session("jp_stock", "7203", dt(2024, 1, 8, 1), holidays=holidays) is True


# ---- rate limit ------------------------------------------------------------
def test_rate_limit_sliding_window(fake_redis):
    now = 1000.0
    assert rate_limit_allow(fake_redis, "k", 2, now=now) is True
    assert rate_limit_allow(fake_redis, "k", 2, now=now) is True
    assert rate_limit_allow(fake_redis, "k", 2, now=now) is False
    # ウィンドウが過ぎれば再び許可
    assert rate_limit_allow(fake_redis, "k", 2, now=now + 61) is True
